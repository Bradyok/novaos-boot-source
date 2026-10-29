#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""The gates build.sh runs over a finished build of the Rigol RK3399 first stage. Each one fails the
build; every result goes into build-manifest.json. U-Boot proper is Step 1's (the board rigol/nova_rk3399,
its mainline flavor), so check-uboot-rk3399.py --flavor mainline holds it too (build.sh runs it on each
family's build and these gates read its report).

  card         idbloader.img + u-boot.itb write nothing but LBA 64-535 (the stock IDB's own sectors)
               and the chosen gap (LBA 2048-7167): sizes against their slots, the itb's sector as
               SPL reads it (CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR), SPL_PAD_TO, and
               u-boot-rockchip.bin = idbloader + padding + itb exactly.
  config       what must be on and off in every family's .config (no ADC download key, no
               watchdog driver, no Android BCB/A-B code, no saved environment, no external TPL, the
               LPDDR4 driver, BL31 given the FDT, quiet loaders, every stage timed).
  reboot-flag  nova_stock.c compiled on the host and run over every low byte: 1 and 0x1888aaff
               are Loader, 3 recovery, 6/7 (stock "wipe data") and everything else normal; the
               board code writes PMUGRF OS_REG0 only with the normal, Loader and recovery flags; no
               binary carries 0x5242c306/0x5242c307 or a --wipe string.
  frames       the front panel's three frames, and their CRC-7/MMC.
  misc         misc is read only at misc + 16 KiB (NOVA_MISC_BCB_LBA), and the board code writes
               nothing to the card: no block write anywhere in it (Loader mode's Rockusb is the only
               writer, and only a host on the USB device port drives it).
  pstore       nothing SPL loads from the card, nor BL31, nor SPL's own stack and BSS, lands in
               0x110000-0x1effff (the ramoops area NovaOS reads after a warm reboot); the bootstage
               stash and SPL's record of UART0's open sit in the free RAM 0x1f0000-0x1fffff, clear of
               every one of those and of U-Boot proper, above the first MiB BL31 makes secure.
  uboot        check-uboot-rk3399.py passed on each family's U-Boot proper.
  maskrom      usb471 is the TPL, usb472 starts with SPL and carries the FIT, and the loader file
               decodes (mkloader.py) and parses (rkdeveloptool's own code, ldr_check) to exactly
               those bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import mkloader  # noqa: E402

SECTOR = 512
PSTORE = (0x110000, 0x1F0000)          # ramoops@110000, 0xe0000 bytes (the NovaOS device tree)
FREE = (0x1F0000, 0x200000)            # below U-Boot's and Linux's RAM (bank 0 starts at 2 MiB), above pstore
SECURE_END = 0x100000                  # BL31 makes DRAM 0-1 MiB secure (TF-A rk3399 TZRAM_BASE/SIZE)
HANDOFF_BYTES = 16                     # nova_uart0.c struct nova_handoff
FDT_MAGIC = b"\xd0\x0d\xfe\xed"

MUST_ON = ["CONFIG_TPL", "CONFIG_SPL", "CONFIG_ROCKCHIP_RK3399", "CONFIG_TARGET_NOVA_RIGOL_RK3399",
           "CONFIG_NOVA_RK3399_MAINLINE", "CONFIG_RAM_ROCKCHIP_LPDDR4", "CONFIG_ROCKCHIP_MASKROM_IMAGE",
           "CONFIG_SPL_RAM_DEVICE", "CONFIG_TPL_ROCKCHIP_BACK_TO_BROM", "CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_USE_SECTOR",
           "CONFIG_ENV_IS_NOWHERE", "CONFIG_MMC_DW_ROCKCHIP", "CONFIG_CMD_ROCKUSB", "CONFIG_USB_FUNCTION_ROCKUSB",
           "CONFIG_BOARD_LATE_INIT", "CONFIG_BOARD_EARLY_INIT_F", "CONFIG_SHA1", "CONFIG_CMD_BOOTI", "CONFIG_CMD_BOOTM",
           "CONFIG_LZ4", "CONFIG_SILENT_CONSOLE", "CONFIG_SPL_SILENT_CONSOLE", "CONFIG_TPL_SILENT_CONSOLE",
           "CONFIG_BOOTSTAGE", "CONFIG_TPL_BOOTSTAGE", "CONFIG_SPL_BOOTSTAGE", "CONFIG_BOOTSTAGE_STASH",
           "CONFIG_BOOTSTAGE_FDT",
           # TPL's only console: its error lines (a failed LPDDR4 training) on UART2
           "CONFIG_DEBUG_UART"]
MUST_OFF = ["CONFIG_ROCKCHIP_EXTERNAL_TPL", "CONFIG_ADC", "CONFIG_SPL_ADC", "CONFIG_WDT", "CONFIG_SPL_WDT",
            "CONFIG_CMD_BCB", "CONFIG_ANDROID_AB", "CONFIG_BOOTMETH_ANDROID", "CONFIG_BOOTSTD",
            "CONFIG_ENV_IS_IN_MMC", "CONFIG_ENV_IS_IN_FAT", "CONFIG_ENV_IS_IN_EXT4", "CONFIG_ENV_IS_IN_SPI_FLASH",
            "CONFIG_SPL_ATF_NO_PLATFORM_PARAM", "CONFIG_CMD_SAVEENV", "CONFIG_USB_FUNCTION_FASTBOOT",
            "CONFIG_CMD_FASTBOOT", "CONFIG_USE_PREBOOT", "CONFIG_SPL_OS_BOOT", "CONFIG_NET",
            "CONFIG_FASTBOOT_FLASH", "CONFIG_CMD_UMS", "CONFIG_CMD_USB_MASS_STORAGE", "CONFIG_CMD_WRITE",
            "CONFIG_CMD_MMC", "CONFIG_ROCKCHIP_DOWNLOAD_KEY", "CONFIG_DEBUG_UART_ANNOUNCE", "CONFIG_SPL_BANNER_PRINT",
            "CONFIG_TPL_BANNER_PRINT", "CONFIG_SPL_DISPLAY_PRINT", "CONFIG_RAM_ROCKCHIP_DEBUG",
            "CONFIG_SYSRESET_PRINT_RESETINFO", "CONFIG_HANDOFF"]
VALUES = {"CONFIG_ROCKCHIP_BOOT_MODE_REG": "0xff320300", "CONFIG_BAUDRATE": "115200",
          "CONFIG_BOOTSTAGE_STASH_ADDR": "0x1f0000", "CONFIG_BOOTSTAGE_STASH_SIZE": "0x1000",
          "CONFIG_NOVA_SPL_HANDOFF_ADDR": "0x1f1000", "CONFIG_NOVA_MISC_BCB_LBA": "0x8020",
          "CONFIG_NOVA_RUNSTOP_WINDOW_MS": "50", "CONFIG_BOOTCOMMAND": "nova boot"}


def load_env(path: Path) -> dict:
    vals = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^([A-Z0-9_]+)="?(.*?)"?$', line.strip())
        if m:
            vals[m.group(1)] = m.group(2)
    return vals


def kconfig(path: Path) -> dict:
    cfg = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2).strip('"')
    return cfg


def num(cfg: dict, key: str) -> int:
    return int(cfg[key], 0)


def digests(p: Path) -> dict:
    b = p.read_bytes()
    return {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}


class Gate:
    def __init__(self):
        self.results = {}
        self.failed = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results[name] = {"ok": bool(ok), "detail": detail}
        print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
        if not ok:
            self.failed.append(name)


def card_gate(g: Gate, fam: str, d: Path, cfg: dict, pins: dict):
    idb_lba, idb_end = int(pins["IDB_LBA"]), int(pins["IDB_END_LBA"])
    itb_lba, gap_end = int(pins["ITB_LBA"]), int(pins["GAP_END_LBA"])
    idb, itb, whole = (d / "idbloader.img").read_bytes(), (d / "u-boot.itb").read_bytes(), (d / "u-boot-rockchip.bin").read_bytes()
    idb_sec, itb_sec = -(-len(idb) // SECTOR), -(-len(itb) // SECTOR)
    g.check(f"{fam}.card.idbloader_fits_lba_64_535", idb_lba + idb_sec <= idb_end,
            f"{len(idb)} B = {idb_sec} sectors, LBA {idb_lba}-{idb_lba + idb_sec - 1} (slot {idb_lba}-{idb_end - 1}, "
            f"{(idb_end - idb_lba) * SECTOR - len(idb)} B spare)")
    g.check(f"{fam}.card.itb_fits_gap", itb_lba + itb_sec <= gap_end,
            f"{len(itb)} B = {itb_sec} sectors, LBA {itb_lba}-{itb_lba + itb_sec - 1} (gap to {gap_end - 1}, "
            f"{(gap_end - itb_lba) * SECTOR - len(itb)} B spare)")
    g.check(f"{fam}.card.itb_sector_as_spl_reads_it",
            num(cfg, "CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR") == itb_lba,
            f"CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR={cfg.get('CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR')}, never 0x4000 (Rigol's uboot)")
    g.check(f"{fam}.card.spl_pad_to", num(cfg, "CONFIG_SPL_PAD_TO") == (itb_lba - idb_lba) * SECTOR,
            f"CONFIG_SPL_PAD_TO={cfg.get('CONFIG_SPL_PAD_TO')}")
    g.check(f"{fam}.card.idbloader_is_rksd_rk33", idb[0x800:0x804] == b"RK33", "the TPL after the 4-sector rksd header")
    off = (itb_lba - idb_lba) * SECTOR
    g.check(f"{fam}.card.whole_image_is_idb_pad_itb",
            whole[:len(idb)] == idb and whole[off:off + len(itb)] == itb and len(whole) == off + len(itb)
            and set(whole[len(idb):off]) <= {0xFF},
            f"u-boot-rockchip.bin {len(whole)} B written at LBA {idb_lba} ends at LBA {idb_lba + -(-len(whole) // SECTOR) - 1}")
    g.check(f"{fam}.card.itb_is_fit", itb[:4] == FDT_MAGIC)
    txt = (d / "u-boot.itb.txt").read_text()
    g.check(f"{fam}.card.fit_has_bl31_and_uboot",
            "ARM Trusted Firmware" in txt and "U-Boot" in txt and not re.search(r"OS:\s+(tee|Trusted Execution)", txt, re.I),
            "FIT: U-Boot + TF-A BL31 (+ the DT); no OP-TEE")
    return {"idbloader.img": {"lba": idb_lba, "sectors": idb_sec, "slot_end_lba": idb_end - 1},
            "u-boot.itb": {"lba": itb_lba, "sectors": itb_sec, "slot_end_lba": gap_end - 1}}


def config_gate(g: Gate, fam: str, cfg: dict):
    missing = [k for k in MUST_ON if cfg.get(k) != "y"]
    present = [k for k in MUST_OFF if cfg.get(k) == "y"]
    g.check(f"{fam}.config.must_be_on", not missing, ", ".join(missing) or f"{len(MUST_ON)} options")
    g.check(f"{fam}.config.must_be_off", not present, ", ".join(present) or f"{len(MUST_OFF)} options")
    bad = {k: cfg.get(k) for k, v in VALUES.items() if cfg.get(k) != v}
    g.check(f"{fam}.config.values", not bad, json.dumps(bad) if bad else
            "TPL honors 0xEF08A53C (back to the BootROM's download mode) from PMUGRF OS_REG0; U-Boot proper "
            "never writes it (no download key); " + ", ".join(f"{k[7:]}={v}" for k, v in VALUES.items()))
    g.check(f"{fam}.config.family", cfg.get("CONFIG_DEFAULT_DEVICE_TREE") == f"rk3399-nova-rigol-{fam}",
            cfg.get("CONFIG_DEFAULT_DEVICE_TREE", ""))


def stock_gate(g: Gate, board: Path, cc: str):
    """nova_stock.c built on this host: the reboot-flag table and the frames, as the U-Boot build compiles them."""
    test_c = r'''
#include <stdio.h>
#include "nova_stock.h"
int main(void) {
    int bad = 0;
    for (unsigned b = 0; b < 256; b++) {
        enum nova_flag_mode m = nova_flag_mode(0x5242c300u | b);
        enum nova_flag_mode want = b == 1 ? NOVA_FLAG_LOADER : b == 3 ? NOVA_FLAG_RECOVERY : NOVA_FLAG_NORMAL;
        if (m != want) { printf("low byte %u -> %s\n", b, nova_flag_mode_name(m)); bad++; }
        if (nova_flag_mode(0xc3524200u | b) != NOVA_FLAG_NORMAL) { printf("consumed %u\n", b); bad++; }
    }
    if (nova_flag_mode(0x5242c306u) != NOVA_FLAG_NORMAL) bad++;
    if (nova_flag_mode(0x5242c307u) != NOVA_FLAG_NORMAL) bad++;
    if (nova_flag_mode(0x1888aaffu) != NOVA_FLAG_LOADER) bad++;
    if (nova_flag_mode(0xef08a53cu) != NOVA_FLAG_NORMAL) bad++;
    if (nova_flag_mode(0u) != NOVA_FLAG_NORMAL) bad++;
    printf("%d wrong\n", bad);
    return bad != 0;
}
'''
    with tempfile.TemporaryDirectory() as t:
        src = Path(t) / "t.c"
        src.write_text(test_c)
        exe = Path(t) / "t"
        r = subprocess.run([cc, "-std=gnu11", "-Wall", "-Werror", "-I", str(board), "-o", str(exe), str(src),
                            str(board / "nova_stock.c")], capture_output=True, text=True)
        if r.returncode:
            g.check("reboot_flag.decoder_table", False, r.stderr.strip())
        else:
            r = subprocess.run([str(exe)], capture_output=True, text=True)
            g.check("reboot_flag.decoder_table", r.returncode == 0,
                    "1 and 0x1888aaff -> loader, 3 -> recovery, 6/7 and the rest -> normal: " + r.stdout.strip().splitlines()[-1])
    src = "\n".join((board / f).read_text() for f in ("nova_rk3399.c", "nova_stock.c", "nova_uart0.c", "nova_chain.c"))
    writes = sorted({m.group(1) for m in re.finditer(r"writel\(([^,]+),\s*PMUGRF_OS_REG0\)", src)})
    g.check("reboot_flag.os_reg0_written_only_with_the_three_flags", set(writes) <= {"FLAG_NORMAL", "FLAG_LOADER",
                                                                                     "FLAG_RECOVERY"},
            "writel(..., PMUGRF_OS_REG0) with " + ", ".join(writes))
    g.check("reboot_flag.no_wipe_handling_in_source",
            not re.search(r"0x5242c30[67]|wipe_data|wipe_all", src.replace("--wipe_data", ""), re.I),
            "no 0x5242c306/07 constant and no wipe path in the board code")


def crc7_mmc(data: bytes) -> int:
    crc = 0
    for b in data:
        for i in range(7, -1, -1):
            top = (crc >> 6) & 1
            crc = (crc << 1) & 0x7F
            if top ^ ((b >> i) & 1):
                crc ^= 0x09
    return crc


def frames_gate(g: Gate, board: Path):
    """The three front-panel frames in the board code: FA b1 b2 crc AF with crc = CRC-7/MMC(b1, b2)
    (the front panel's frame format), and exactly the three the board needs."""
    want = {"nova_frame_runstop_press": "fa080c34af", "nova_frame_runstop_release": "fa080058af",
            "nova_frame_power_mode_auto": "fa05050aaf"}
    src = (board / "nova_stock.c").read_text()
    got = {}
    for name in want:
        m = re.search(name + r"\[5\] = \{([^}]*)\}", src)
        got[name] = bytes(int(x, 16) for x in m.group(1).replace(" ", "").split(",")) if m else b""
    ok = all(got[n].hex() == want[n] and got[n][0] == 0xFA and got[n][4] == 0xAF and got[n][3] == crc7_mmc(got[n][1:3])
             for n in want)
    g.check("frames.front_panel", ok, ", ".join(f"{n}={got[n].hex()}" for n in want))


def binary_gate(g: Gate, fam: str, d: Path):
    bad = {}
    for name in ("u-boot.bin", "u-boot-spl.bin", "u-boot-tpl.bin"):
        blob = (d / name).read_bytes()
        hits = [k for k, pat in (("0x5242c306", struct.pack("<I", 0x5242C306)), ("0x5242c307", struct.pack("<I", 0x5242C307)),
                                 ("--wipe", b"--wipe"), ("update-bootloader", b"update-bootloader")) if pat in blob]
        if hits:
            bad[name] = hits
    g.check(f"{fam}.reboot_flag.no_wipe_values_in_binaries", not bad, json.dumps(bad) if bad else "u-boot, SPL, TPL")
    spl = (d / "u-boot-spl.bin").read_bytes()
    g.check(f"{fam}.binary.spl_never_builds_0xef08a53c", struct.pack("<I", 0xEF08A53C) not in spl,
            "SPL loads U-Boot; only TPL compares OS_REG0 with BOOT_BROM_DOWNLOAD (save_boot_params)")


def misc_gate(g: Gate, fam: str, d: Path, cfg: dict, board: Path):
    bcb = num(cfg, "CONFIG_NOVA_MISC_BCB_LBA")
    g.check(f"{fam}.misc.bcb_at_misc_plus_16k", bcb == 0x8000 + 32,
            f"NOVA_MISC_BCB_LBA={cfg.get('CONFIG_NOVA_MISC_BCB_LBA')} (misc LBA 0x8000 + 32 sectors); misc offset 0 is never read")
    src = (board / "nova_rk3399.c").read_text()
    misc_reads = re.findall(r"nova_read\(desc, ([A-Z_]*MISC[A-Z_]*)", src)
    g.check(f"{fam}.misc.read_only_there", misc_reads == ["CONFIG_NOVA_MISC_BCB_LBA"],
            f"nova_read of misc: {misc_reads}")
    allsrc = "\n".join((board / f).read_text() for f in ("nova_rk3399.c", "nova_stock.c", "nova_uart0.c", "nova_chain.c"))
    writes = re.findall(r"\b(blk_dwrite|blk_write|blk_derase|blk_erase|mmc_bwrite|mmc_berase|mmc write|mmc erase|"
                        r"saveenv|env_save|bcb |fastboot|ums )", allsrc)
    g.check(f"{fam}.misc.board_code_writes_no_block", not writes, ", ".join(writes) or "no block write or erase")
    rockusb = [l.strip() for l in src.splitlines() if "rockusb" in l and "run_command" in l]
    body = re.search(r"static void __noreturn nova_loader_mode\(const char \*why\)\n\{\n(.*?)^\}\n", src, re.M | re.S)
    g.check(f"{fam}.misc.rockusb_only_in_loader_mode", len(rockusb) == 1 and body and "rockusb" in body.group(1),
            "; ".join(rockusb))
    env = dict(l.partition("=")[::2] for l in (d / "u-boot-initial-env").read_text().splitlines() if "=" in l)
    g.check(f"{fam}.env.bootcmd_and_silent", env.get("bootcmd") == "nova boot" and env.get("silent") == "1",
            f"bootcmd={env.get('bootcmd')!r} silent={env.get('silent')!r}")


def pstore_gate(g: Gate, fam: str, d: Path, cfg: dict, bl31: Path, readelf: str):
    lo, hi = PSTORE
    ranges = []
    txt = (d / "u-boot.itb.txt").read_text()
    for blk in txt.split("Image ")[1:]:
        name = blk.split("\n", 1)[0].strip()
        m_size = re.search(r"Data Size:\s+(\d+) Bytes", blk)
        for m in re.finditer(r"(Load Address|Segment .*? address):\s+0x([0-9a-f]+)", blk):
            ranges.append((f"itb {name}", int(m.group(2), 16), int(m_size.group(1)) if m_size else 0))
    out = subprocess.run([readelf, "-lW", str(bl31)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        f = line.split()
        if f and f[0] == "LOAD":
            ranges.append(("bl31 PT_LOAD", int(f[3], 16), int(f[5], 16)))
    spl = (d / "u-boot-spl.bin").stat().st_size
    for key, size in (("CONFIG_SPL_TEXT_BASE", spl), ("CONFIG_SPL_BSS_START_ADDR", int(cfg.get("CONFIG_SPL_BSS_MAX_SIZE", "0"), 0)),
                      ("CONFIG_SPL_STACK_R_ADDR", 0), ("CONFIG_SPL_STACK", 0), ("CONFIG_TEXT_BASE", (d / "u-boot.bin").stat().st_size)):
        if key in cfg:
            ranges.append((key, int(cfg[key], 0), size))
    if "CONFIG_SPL_STACK_R_ADDR" in cfg and "CONFIG_SPL_STACK_R_MALLOC_SIMPLE_LEN" in cfg:
        top = int(cfg["CONFIG_SPL_STACK_R_ADDR"], 0)
        ranges.append(("SPL stack_r + malloc", top - int(cfg["CONFIG_SPL_STACK_R_MALLOC_SIMPLE_LEN"], 0), int(cfg["CONFIG_SPL_STACK_R_MALLOC_SIMPLE_LEN"], 0)))
    # the Maskrom 472 (SPL + the FIT) where the BootROM puts it
    ranges.append(("usb472", int(cfg.get("CONFIG_SPL_TEXT_BASE", "0"), 0), (d / "u-boot-rockchip-usb472.bin").stat().st_size))
    hits = [f"{n} 0x{a:x}+0x{s:x}" for n, a, s in ranges if a < hi and a + max(s, 1) > lo]
    g.check(f"{fam}.pstore.untouched_on_card_boot", not hits,
            "; ".join(hits) if hits else f"{len(ranges)} ranges checked against 0x{lo:x}-0x{hi - 1:x}")
    # the bootstage stash and SPL's record of UART0's open: in the free RAM, and nothing above lands on them
    own = [("bootstage stash", num(cfg, "CONFIG_BOOTSTAGE_STASH_ADDR"), num(cfg, "CONFIG_BOOTSTAGE_STASH_SIZE")),
           ("UART0 open record", num(cfg, "CONFIG_NOVA_SPL_HANDOFF_ADDR"), HANDOFF_BYTES)]
    inside = all(FREE[0] <= a and a + s <= FREE[1] and a >= SECURE_END for _, a, s in own)
    apart = own[0][1] + own[0][2] <= own[1][1] or own[1][1] + own[1][2] <= own[0][1]
    clash = [f"{n} 0x{a:x}+0x{s:x} on {on}" for on, oa, os_ in own for n, a, s in ranges
             if a < oa + os_ and a + max(s, 1) > oa]
    g.check(f"{fam}.pstore.stash_and_record_in_free_ram", inside and apart and not clash,
            "; ".join(clash) if clash else ", ".join(f"{n} 0x{a:x}-0x{a + s - 1:x}" for n, a, s in own) +
            f" inside 0x{FREE[0]:x}-0x{FREE[1] - 1:x} (non-secure, below U-Boot's and Linux's RAM), apart, "
            f"and clear of the {len(ranges)} ranges above")
    return [{"what": n, "start": hex(a), "bytes": s} for n, a, s in ranges + own]


def uboot_gate(g: Gate, fam: str, d: Path):
    rep = d / "uboot-gates.json"
    r = json.loads(rep.read_text()) if rep.is_file() else {}
    failed = [x["gate"] for x in r.get("gates", []) if not x["ok"]]
    g.check(f"{fam}.uboot.check_uboot_rk3399", bool(r.get("ok")) and r.get("flavor") == "mainline" and not failed,
            f"{len(r.get('gates', []))} gates of check-uboot-rk3399.py --flavor mainline" + (
                f"; failed: {', '.join(failed)}" if failed else ""))


def maskrom_gate(g: Gate, fam: str, d: Path, cfg: dict):
    tpl, spl = (d / "u-boot-tpl.bin").read_bytes(), (d / "u-boot-spl.bin").read_bytes()
    u471, u472 = (d / "u-boot-rockchip-usb471.bin").read_bytes(), (d / "u-boot-rockchip-usb472.bin").read_bytes()
    g.check(f"{fam}.maskrom.usb471_is_tpl", u471[:len(tpl)] == tpl, f"{len(u471)} B")
    fit_at = u472.find(FDT_MAGIC, len(spl))
    # binman writes its image symbols (the payload's offset and size) into SPL's copy: a few words differ.
    diff = sum(1 for a, b in zip(spl, u472[:len(spl)]) if a != b)
    itb = (d / "u-boot.itb").read_bytes()
    # The FIT binman builds for the payload has its own header (other data positions); the external data after
    # it, every image SPL loads, must be u-boot.itb's byte for byte.
    hdr = -(-struct.unpack_from(">I", itb, 4)[0] // 512) * 512
    same_data = len(u472) - fit_at == len(itb) and u472[fit_at + hdr:] == itb[hdr:]
    g.check(f"{fam}.maskrom.usb472_is_spl_then_fit", diff <= 64 and fit_at >= len(spl) and same_data,
            f"{len(u472)} B: SPL ({diff} bytes of binman symbols differ), the FIT at +0x{fit_at:x} with u-boot.itb's "
            f"images byte for byte after its 0x{hdr:x}-byte header")
    # The BootROM puts the 472 at SPL_TEXT_BASE (DRAM 0); below 0x110000 it leaves the pstore records alone too.
    base = int(cfg.get("CONFIG_SPL_TEXT_BASE", "0"), 0)
    g.check(f"{fam}.maskrom.usb472_below_pstore", base + len(u472) <= PSTORE[0],
            f"0x{base:x}-0x{base + len(u472) - 1:x}")
    loader = d / f"rigol-{fam}-maskrom-loader.bin"
    ent = mkloader.decode(loader.read_bytes())
    pad = lambda b: b + bytes(-len(b) % mkloader.ENTRY_ALIGN)  # noqa: E731
    g.check(f"{fam}.maskrom.loader_decodes_to_the_images",
            ent["471_0"]["data"] == pad(u471) and ent["472_0"]["data"] == pad(u472) and len(ent) == 2,
            f"{loader.name}: 471 {len(u471)} B (+{ent['471_0']['delay_ms']} ms), 472 {len(u472)} B, no loader entries")
    lc = d / "ldr_check"
    sent = {k: (lc / f"{k}_0.bin").read_bytes() for k in ("471", "472")} if lc.is_dir() else {}
    g.check(f"{fam}.maskrom.rkdeveloptool_sends_these_bytes",
            bool(sent) and mkloader.rc4(sent["471"]) == pad(u471) and mkloader.rc4(sent["472"]) == pad(u472),
            "rkdeveloptool's CRKImage/CRKBoot accepted the file; the entries it would send RC4-decode to usb471/usb472")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pins", type=Path, required=True)
    ap.add_argument("--families", required=True)
    ap.add_argument("--board", type=Path, required=True, help="the patched tree's board/rigol/nova_rk3399")
    ap.add_argument("--lane-id", required=True)
    ap.add_argument("--cc", default="gcc")
    ap.add_argument("--bl31", type=Path, required=True)
    ap.add_argument("--readelf", required=True)
    ap.add_argument("--source-date-epoch", type=int, required=True)
    ap.add_argument("--patches", type=Path, nargs="+", required=True, help="the patch directories, in the order applied")
    ap.add_argument("--builder", type=Path, required=True)
    ap.add_argument("--compilers", nargs="*", default=[])
    args = ap.parse_args()
    pins = load_env(args.pins)
    g = Gate()
    stock_gate(g, args.board, args.cc)
    frames_gate(g, args.board)
    manifest = {"lane_id": args.lane_id, "pins": pins, "source_date_epoch": args.source_date_epoch,
                "compilers": args.compilers,
                "patches": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for d in args.patches for p in sorted(d.glob("*.patch"))},
                "tf_a": {p.name: digests(p) for p in sorted((args.out / "tf-a").iterdir())},
                "families": {}, "hardware_boot_verified": False}
    for fam in args.families.split():
        d = args.out / fam
        cfg = kconfig(d / "final.config")
        card = card_gate(g, fam, d, cfg, pins)
        config_gate(g, fam, cfg)
        binary_gate(g, fam, d)
        misc_gate(g, fam, d, cfg, args.board)
        ranges = pstore_gate(g, fam, d, cfg, args.bl31, args.readelf)
        uboot_gate(g, fam, d)
        maskrom_gate(g, fam, d, cfg)
        manifest["families"][fam] = {
            "build_id": cfg.get("CONFIG_NOVA_BUILD_ID", ""), "card": card, "memory_ranges": ranges,
            "artifacts": {f.name: digests(f) for f in sorted(d.iterdir()) if f.is_file()},
        }
    manifest["gates"] = g.results
    (args.out / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with open(args.out / "SHA256SUMS", "w", newline="\n") as f:
        for p in sorted(args.out.rglob("*")):
            if p.is_file() and p.name not in ("SHA256SUMS", "build.log"):
                f.write(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(args.out).as_posix()}\n")
    print(f"{len(g.results) - len(g.failed)}/{len(g.results)} gates pass")
    return 1 if g.failed else 0


if __name__ == "__main__":
    sys.exit(main())
