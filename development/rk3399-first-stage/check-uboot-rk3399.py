#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""The gates of the NovaOS RK3399 U-Boot proper: one build directory, one family, one flavor.

    python3 check-uboot-rk3399.py --build O --family dho900|mho900 --flavor kexec-test|chainload|mainline \
        [--cross aarch64-linux-gnu-] [--json report.json]

The mainline flavor is Step 2's U-Boot proper (build.sh builds it with TPL, SPL and BL31): the same gates,
with the exceptions its job needs named in FLAVOR (Loader mode is its Rockusb, so USB, Rockusb and the MMC write
path are linked; TPL honors the BootROM download flag, so the boot-mode register is set) and the rest held as
for Step 1.

Every gate reads the build's own outputs (.config, u-boot ELF, u-boot.bin, u-boot.dtb); none trusts the defconfig it
came from. Exit 0 only when every gate passes; the report lists each with what it read. Writes no device.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import fdt_tree  # noqa: E402

FORBIDDEN_BYTES = bytes.fromhex("3ca508ef")   # BOOT_BROM_DOWNLOAD 0xEF08A53C, little-endian: reset into Maskrom
LOADER_FLAG = 0x5242C301                      # the stock reboot flag for Rockusb (chainload's last rung only)
MAX_BIN = 2 << 20

# --- .config ---------------------------------------------------------------------------------------------------
# On in both flavors.
NEED_Y = ("ARM64", "ROCKCHIP_RK3399", "TARGET_NOVA_RIGOL_RK3399", "ROCKCHIP_LINUX_KERNEL_IMAGE_HEADER",
          "LINUX_KERNEL_IMAGE_HEADER", "POSITION_INDEPENDENT", "INIT_SP_RELATIVE", "SKIP_LOWLEVEL_INIT",
          "OF_CONTROL", "OF_SEPARATE", "ENV_IS_NOWHERE", "MMC_DW", "MMC_DW_ROCKCHIP",
          "LZ4", "CONSOLE_RECORD", "SILENT_CONSOLE", "SYSRESET", "USE_BOOTCOMMAND",
          # silent=1 quiets U-Boot only: without it bootm turns the kernel's console= into console=ttynull
          # (fixup_silent_linux), and the booted NovaOS loses its serial console
          "SILENT_U_BOOT_ONLY")
# Off in every flavor, each for a recovery reason:
NEED_OFF = (
    # the watchdog: a U-Boot that feeds it would never reset when it hangs; nothing may stop it either
    "WDT", "WATCHDOG", "WATCHDOG_AUTOSTART", "DESIGNWARE_WATCHDOG", "ROCKCHIP_WATCHDOG", "CYCLIC_WATCHDOG",
    # Maskrom: the download-key check writes BOOT_BROM_DOWNLOAD and resets into a Maskrom that waits for ever
    "ADC", "SARADC_ROCKCHIP", "CMD_ROCKUSB", "USB_FUNCTION_ROCKUSB", "USB_FUNCTION_FASTBOOT", "CMD_FASTBOOT",
    # card writes: no MMC write path, no saved environment, no bootcount, no EFI variables, no BCB
    "MMC_WRITE", "ENV_IS_IN_MMC", "ENV_IS_IN_FAT", "ENV_IS_IN_EXT4", "ENV_IS_IN_SPI_FLASH", "CMD_SAVEENV",
    "BOOTCOUNT_LIMIT", "EFI_LOADER", "CMD_BCB", "ANDROID_AB", "BOOTMETH_ANDROID", "CMD_GPT", "CMD_MMC",
    "CMD_USB_MASS_STORAGE",
    # rails, pins, buses: no PMIC/regulator/PWM/SPI/PCI/USB/video/I2C/GPIO driver, no I/O-domain switching
    "PMIC_RK8XX", "REGULATOR_RK8XX", "DM_REGULATOR_GPIO", "REGULATOR_PWM", "DM_PWM", "PWM_ROCKCHIP",
    "SPI", "DM_SPI", "ROCKCHIP_SPI", "PCI", "USB", "VIDEO", "DM_VIDEO", "DM_I2C", "SYS_I2C_ROCKCHIP",
    "ROCKCHIP_GPIO", "ROCKCHIP_IODOMAIN", "LED", "POWER_DOMAIN",
    # the SD card stays in SD high speed at 3.3 V, as Linux runs it (a card left in UHS the BootROM cannot read)
    "MMC_UHS_SUPPORT", "MMC_HS200_SUPPORT", "MMC_HS400_SUPPORT", "MMC_SDHCI",
    # no clock re-init (HANDOFF without SPL data runs rkclk_init), no efuse/RNG/timer block touched
    "HANDOFF", "ROCKCHIP_EFUSE", "MISC_INIT_R", "DM_RNG", "RNG_ROCKCHIP", "ROCKCHIP_TIMER",
    # nothing else boots, nothing listens
    "NET", "CMD_NET", "BOOTSTD", "DEBUG_UART", "TPL", "AUTOBOOT_KEYED",
    # print_resetinfo() probes the sysreset device before relocation for nothing on this SoC (PSCI reports
    # no reset cause here)
    "SYSRESET_PRINT_RESETINFO")
# PMIC_/REGULATOR_ options that are no driver: the uclass's own switches.
PMIC_NOT_DRIVERS = ("PMIC_CHILDREN",)
# The card's places (the PARM table + 0x2000; the slot is `backup` + 32 MiB), the RAM map and the FIT limits.
NEED_VALUE = {"BOOTDELAY": "-2", "BAUDRATE": "115200", "NR_DRAM_BANKS": "2",
              "NOVA_PMSG_ADDR": "0x1b0000", "NOVA_PMSG_SIZE": "0x40000", "NOVA_RECOVERY_LBA": "0x2e000",
              "NOVA_BACKUP_LBA": "0x4e000", "NOVA_SLOT_LBA": "0x5e000", "NOVA_SLOT_SECTORS": "0x20000",
              "NOVA_BOOT_LBA": "0x1e000", "NOVA_KERNEL_LBA": "0x12000", "NOVA_RESOURCE_LBA": "0xa000",
              "NOVA_PARM_LBA": "0x2000", "NOVA_TRUST_OS_BASE": "0x08400000", "NOVA_TRUST_OS_SIZE": "0x01e00000",
              "NOVA_FIT_CONFIG": '"conf-1"', "NOVA_FIT_MAX": "0x4000000"}
# Per flavor: its option, the others' (off), its bootcmd, what else it needs on, and the board code it must link. The
# chainload arms the watchdog in board_early_init_f and keeps the attempt record (nova_chain.c); the kexec test never
# arms it (its one counter restart is `nova kexec-test`'s). Step 2 (mainline) is the chainload's U-Boot proper with the
# stock loader's duties (nova_stock.c, nova_uart0.c): it arms the watchdog where the stock order reaches a chain image.
# `allow`: options of NEED_OFF a flavor needs on; `allow_symbols`: FORBIDDEN_SYMBOLS it links; `values`: NEED_VALUE
# beside the common ones; `dt_allowed`: compatibles it enables beside DT_ALLOWED.
FLAVOR = {"kexec-test": {"y": "NOVA_RK3399_KEXEC_TEST", "off": ("NOVA_RK3399_CHAINLOAD", "NOVA_RK3399_MAINLINE"),
                         "bootcmd": "nova kexec-test; nova reset", "need_y": ("SAVE_PREV_BL_FDT_ADDR", "SYSRESET_PSCI"),
                         "need_off": ("BOARD_EARLY_INIT_F",), "symbols": ("get_prev_bl_fdt_addr",),
                         "values": {"ROCKCHIP_BOOT_MODE_REG": "0x0"}, "strings": ()},
          "chainload": {"y": "NOVA_RK3399_CHAINLOAD", "off": ("NOVA_RK3399_KEXEC_TEST", "NOVA_RK3399_MAINLINE"),
                        "bootcmd": "nova boot", "values": {"ROCKCHIP_BOOT_MODE_REG": "0x0"},
                        # SUPPORT_RAW_INITRD: `booti kernel ramdisk:size fdt` of a plain NovaOS image and of the
                        # stock firmware (their ramdisks are raw); without it booti refuses them ("Wrong Ramdisk
                        # Image Format": the bench check of the stock rung, 2026-10-27)
                        # the I-cache, the MMU and the D-cache on before relocation (mach_cpu_init, its tables
                        # nova_early_tlb; patches/0005): board_f ran 0.63-0.71 s uncached without them
                        "need_y": ("BOARD_EARLY_INIT_F", "SHA1", "SUPPORT_RAW_INITRD", "SAVE_PREV_BL_FDT_ADDR", "SYSRESET_PSCI"),
                        "need_off": ("SYS_ICACHE_OFF", "SYS_DCACHE_OFF"),
                        "symbols": ("board_early_init_f", "nova_ladder_mode", "nova_ladder_step", "nova_rec_make",
                                    "nova_rec_parse", "nova_parm_cmdline", "nova_stock_cmdline", "mach_cpu_init",
                                    "nova_early_tlb", "get_prev_bl_fdt_addr"),
                        # the stock U-Boot's boot logo kept and handed to the kernel (patches/0006: without it the
                        # kernel reuses the framebuffer the panel is still scanning, r114 on the MHO, 2026-10-28);
                        # its functions are static, so the gate holds their log lines, which only they print
                        "strings": (b"logo: handed to the kernel: drm-logo ", b"logo: the stock U-Boot's boot logo at ")},
          "mainline": {"y": "NOVA_RK3399_MAINLINE", "off": ("NOVA_RK3399_KEXEC_TEST", "NOVA_RK3399_CHAINLOAD"),
                       "bootcmd": "nova boot",
                       # TPL and SPL are this build's; TPL honors BOOT_BROM_DOWNLOAD in OS_REG0, so the register is
                       # named, and U-Boot proper never writes it: no download key (ROCKCHIP_DOWNLOAD_KEY off), and
                       # the byte and immediate gates below hold for it as for every flavor
                       "need_y": ("BOARD_EARLY_INIT_F", "SHA1", "SUPPORT_RAW_INITRD", "TPL", "BOOTSTAGE_STASH",
                                  "TPL_BOOTSTAGE", "SPL_BOOTSTAGE", "CMD_ROCKUSB", "USB_FUNCTION_ROCKUSB"),
                       # x0 from BL31 is no FDT: nothing of the previous stage's is read; TPL and SPL run before
                       # BL31, so the sysreset driver is the CRU's in every phase (`nova reset` asks PSCI itself)
                       "need_off": ("SAVE_PREV_BL_FDT_ADDR", "SYSRESET_PSCI", "SYS_ICACHE_OFF", "SYS_DCACHE_OFF",
                                    "ROCKCHIP_DOWNLOAD_KEY", "DEBUG_UART_ANNOUNCE",
                                    "SPL_BANNER_PRINT",
                                    "TPL_BANNER_PRINT", "RAM_ROCKCHIP_DEBUG", "USB_FUNCTION_FASTBOOT", "CMD_FASTBOOT",
                                    "CMD_USB_MASS_STORAGE", "USB_STORAGE", "USB_KEYBOARD", "CMD_USB"),
                       "values": {"ROCKCHIP_BOOT_MODE_REG": "0xff320300", "NOVA_MISC_BCB_LBA": "0x8020",
                                  "NOVA_PARM_BYTES": "0x3ae", "NOVA_PARM_CRC32": "0x4a377dc5",
                                  "NOVA_RUNSTOP_WINDOW_MS": "50", "NOVA_SPL_HANDOFF_ADDR": "0x1f1000",
                                  "BOOTSTAGE_STASH_ADDR": "0x1f0000", "BOOTSTAGE_STASH_SIZE": "0x1000",
                                  "SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR": "0x800"},
                       # Loader mode: Rockusb on OTG0 (the USB stack and the MMC write path it serves); TPL, whose
                       # only console for its error lines is the debug UART on UART2
                       "allow": ("CMD_ROCKUSB", "USB_FUNCTION_ROCKUSB", "USB", "MMC_WRITE", "TPL", "DEBUG_UART"),
                       "allow_symbols": r"^(rockusb_.*|do_rockusb|dwc3_.*|mmc_bwrite|mmc_berase|mmc_write_blocks)$",
                       "dt_allowed": ("rockchip,rk3399-usb2phy", "rockchip,rk3399-dwc3", "snps,dwc3"),
                       "symbols": ("board_early_init_f", "nova_ladder_mode", "nova_ladder_step", "nova_rec_make",
                                   "nova_rec_parse", "nova_parm_cmdline", "nova_stock_cmdline", "mach_cpu_init",
                                   "nova_early_tlb", "nova_flag_mode", "nova_rx_runstop", "nova_runstop_wait_us",
                                   "nova_stock_order", "nova_bcb_says_recovery", "nova_parm_recovery",
                                   "nova_uart0_opened_at", "nova_uart0_open", "nova_frame_power_mode_auto"),
                       # no stock U-Boot runs before it: no boot logo to hand over
                       "strings": ()}}
# The stock reboot flags that wipe userdata (low byte 6 and 7): nothing here may ever build them.
WIPE_FLAGS = (0x5242C306, 0x5242C307)

# Symbols that would mean a forbidden path was linked in (nm of the ELF).
FORBIDDEN_SYMBOLS = re.compile(
    r"^(set_back_to_bootrom_dnl_flag|rockchip_dnl_mode_check|rockchip_dnl_key_pressed|setup_boot_mode_.*"
    r"|designware_wdt.*|dw_wdt.*|wdt_start|wdt_reset|wdt_expire_now|wdt_stop.*|initr_watchdog"
    r"|mmc_bwrite|mmc_berase|mmc_write_blocks|env_mmc_.*|env_fat_.*|fastboot_.*|rockusb_.*|do_rockusb|ums_.*"
    r"|efi_.*|do_bcb|bcb_.*|android_bootloader_message.*|bootcount_.*|rockchip_efuse.*|rockchip_saradc.*"
    r"|rk8xx_.*|rk808_.*|pwm_.*|rockchip_spi.*|pci_.*|dwc3_.*|video_.*|rkclk_init)$")
NEED_SYMBOLS = ("do_nova", "save_boot_params", "ulz4fn", "rockchip_dram_init_banksize_fixup")

# --- the control DT --------------------------------------------------------------------------------------------
# Compatibles an ENABLED node may carry: the CPU/GIC/timer skeleton, the syscons and clock/pin controllers U-Boot
# needs, the RAM sizer (reads PMUGRF OS_REG2), UART2 and the SD host.
DT_ALLOWED = ("arm,cortex-a53", "arm,cortex-a72", "cache", "arm,idle-state", "arm,cortex-a53-pmu", "arm,cortex-a72-pmu",
              "arm,psci-1.0", "arm,armv8-timer", "fixed-clock", "arm,gic-v3", "arm,gic-v3-its",
              "arm,coresight-cpu-debug", "arm,primecell", "rockchip,rk3399-qos", "syscon", "simple-mfd",
              "rockchip,rk3399-pmu", "rockchip,rk3399-power-controller", "rockchip,rk3399-pmugrf", "rockchip,rk3399-grf",
              "rockchip,rk3399-pmusgrf", "rockchip,rk3399-cic", "rockchip,rk3399-cru", "rockchip,rk3399-pmucru",
              "rockchip,rk3399-pinctrl", "rockchip,gpio-bank", "rockchip,rk3399-dmc", "rockchip,rk3399-dfi",
              "rockchip,iommu", "rockchip,rk3399-uart", "snps,dw-apb-uart", "rockchip,rk3399-dw-mshc",
              "rockchip,rk3288-dw-mshc", "operating-points-v2")
# Pins an enabled device's pinctrl may claim: UART2C (GPIO4_C3/C4), the SD bus (GPIO4_B0-B5) and SD card-detect
# (GPIO0_A7). (bank, pin index 0-31: A0=0 .. D7=31)
PINS_ALLOWED = {(4, 19), (4, 20), (4, 8), (4, 9), (4, 10), (4, 11), (4, 12), (4, 13), (0, 7)}
# What each family must never have driven, and both.
NEVER = {"mho900": {(1, 18): "GPIO1_C2 LMK04826 CS", (4, 5): "GPIO4_A5", (0, 1): "GPIO0_A1 ADC_VCC_EN",
                    (0, 2): "GPIO0_A2 AFE_VCC_EN", (1, 0): "GPIO1_A0 FPGA power", (1, 3): "GPIO1_A3 PCIe PERST#",
                    (1, 7): "UART4/SPI1", (1, 8): "UART4/SPI1", (1, 9): "UART4/SPI1", (1, 10): "UART4/SPI1"},
         "dho900": {(2, 18): "GPIO2_C2 PCIe PERST#/UART0 CTS"},
         "both": {(4, 27): "GPIO4_D3", (0, 0): "GPIO0_A0 PWR_HLD"}}
SDMMC_FORBIDDEN_PROPS = ("vmmc-supply", "vqmmc-supply", "cd-gpios", "sd-uhs-sdr12", "sd-uhs-sdr25", "sd-uhs-sdr50",
                         "sd-uhs-sdr104", "sd-uhs-ddr50", "mmc-hs200-1_8v", "mmc-hs400-1_8v", "mmc-ddr-1_8v",
                         "cap-mmc-highspeed", "mmc-pwrseq")


class Gate:
    def __init__(self):
        self.results = []

    def check(self, name: str, ok: bool, detail) -> None:
        self.results.append({"gate": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.results)


def read_config(path: Path) -> dict:
    cfg = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^CONFIG_(\w+)=(.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2)
    return cfg


def gate_config(g: Gate, cfg: dict, family: str, flavor: str) -> None:
    f = FLAVOR[flavor]
    need = NEED_Y + (f["y"],) + f["need_y"]
    g.check("config: required on", all(cfg.get(k) == "y" for k in need),
            {k: cfg.get(k) for k in need if cfg.get(k) != "y"} or "all y")
    allow = set(f.get("allow", ()))
    on = [k for k in tuple(k for k in NEED_OFF if k not in allow) + f["off"] + f["need_off"] if cfg.get(k) == "y"]
    g.check("config: forbidden off", not on, on or ("all off" + (f"; on for this flavor: {sorted(allow)}" if allow else "")))
    values = {**NEED_VALUE, **f.get("values", {})}
    bad = {k: cfg.get(k) for k, v in values.items() if cfg.get(k) != v}
    g.check("config: values", not bad, bad or values)
    dt = cfg.get("DEFAULT_DEVICE_TREE")
    g.check("config: the family's control DT", dt == f'"rk3399-nova-rigol-{family}"', dt)
    g.check("config: bootcmd", cfg.get("BOOTCOMMAND") == f'"{f["bootcmd"]}"', cfg.get("BOOTCOMMAND"))
    drivers = [k for k in cfg if k.startswith(("PMIC_", "REGULATOR_")) and cfg[k] == "y" and k not in PMIC_NOT_DRIVERS]
    g.check("config: no PMIC or regulator driver (DM_PMIC / DM_REGULATOR_FIXED are the SoC's selects)", not drivers,
            drivers or "none")


def run(cmd: list) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def symbols(cross: str, elf: Path) -> dict:
    out = {}
    for line in run([f"{cross}nm", str(elf)]).splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[2]] = int(parts[0], 16)
    return out


def gate_binary(g: Gate, cross: str, elf: Path, binfile: Path, flavor: str) -> dict:
    data = binfile.read_bytes()
    elf_bytes = elf.read_bytes()
    g.check("bytes: no 3C A5 08 EF (BOOT_BROM_DOWNLOAD) in u-boot.bin or the ELF",
            FORBIDDEN_BYTES not in data and FORBIDDEN_BYTES not in elf_bytes,
            {"u-boot.bin": data.find(FORBIDDEN_BYTES), "u-boot": elf_bytes.find(FORBIDDEN_BYTES)})
    dis = run([f"{cross}objdump", "-d", str(elf)])
    lo, hi = "#0xa53c", "#0xef08, lsl #16"
    g.check("code: no immediate that builds 0xEF08A53C (movz/movk halves)",
            not (lo in dis and hi in dis), {"0xa53c": dis.count(lo), "0xef08<<16": dis.count(hi)})
    flag_lo, flag_hi = "#0xc301", "#0x5242, lsl #16"
    has_flag = flag_lo in dis and flag_hi in dis
    if flavor == "kexec-test":
        g.check("code: the kexec test never builds the Rockusb reboot flag 0x5242C301", not has_flag,
                {"0xc301": dis.count(flag_lo), "0x5242<<16": dis.count(flag_hi)})
    wipe = {f"{v:#x}": dis.count(f"#{v & 0xFFFF:#x}") for v in WIPE_FLAGS
            if f"#{v & 0xFFFF:#x}" in dis and flag_hi in dis}
    wipe_bytes = [f"{v:#x}" for v in WIPE_FLAGS if struct.pack("<I", v) in data]
    g.check("code: never builds a wipe-data reboot flag (0x5242C306 / 0x5242C307)", not wipe and not wipe_bytes,
            {"movz halves": wipe or "none", "bytes": wipe_bytes or "none"})
    syms = symbols(cross, elf)
    allowed = re.compile(FLAVOR[flavor].get("allow_symbols", r"$^"))
    bad = sorted(s for s in syms if FORBIDDEN_SYMBOLS.match(s) and not allowed.match(s))
    linked = sorted(s for s in syms if FORBIDDEN_SYMBOLS.match(s) and allowed.match(s))
    g.check("symbols: no forbidden path linked", not bad,
            bad or ("none" + (f" (this flavor's own: {len(linked)} of {FLAVOR[flavor]['allow_symbols']})" if linked else "")))
    need = NEED_SYMBOLS + FLAVOR[flavor]["symbols"]
    missing = [s for s in need if s not in syms]
    g.check("symbols: the board's code is in", not missing, missing or list(need))
    absent = [t.decode() for t in FLAVOR[flavor]["strings"] if t not in data]
    if FLAVOR[flavor]["strings"]:
        g.check("strings: the boot logo's hand-off is in", not absent,
                absent or [t.decode() for t in FLAVOR[flavor]["strings"]])
    return syms


def gate_header(g: Gate, binfile: Path, syms: dict) -> dict:
    """The arm64 Image header (kexec_file_load's image_load, arch/arm64/kernel/kexec_image.c; the stock U-Boot's
    check_kernel), and that image_size covers the file (kexec_core.c sanity_check_segment_list: bufsz <= memsz)."""
    data = binfile.read_bytes()
    insn, = struct.unpack_from("<I", data, 0)
    text_offset, image_size, flags = struct.unpack_from("<QQQ", data, 8)
    facts = {"bytes": len(data), "first_insn": hex(insn), "text_offset": hex(text_offset),
             "image_size": hex(image_size), "flags": hex(flags), "magic": data[0x38:0x3C].hex()}
    g.check("header: arm64 Image magic, a branch first", data[0x38:0x3C] == b"ARM\x64" and insn >> 26 == 0b000101, facts)
    g.check("header: text_offset 0, flags LE / 4K pages / anywhere (0xa)", text_offset == 0 and flags == 0xA, facts)
    g.check("header: image_size covers the whole file (kexec memsz >= bufsz)", image_size >= len(data), facts)
    g.check("header: size bound (2 MiB)", len(data) <= MAX_BIN and image_size <= 4 * MAX_BIN, facts)
    start, end = syms.get("_start"), syms.get("_end")
    if start is not None and end is not None:
        facts["_end_offset"] = hex(end - start)
        dtb_at = end - start
        g.check("header: the control DT sits at _end (OF_SEPARATE)", data[dtb_at:dtb_at + 4] == b"\xd0\x0d\xfe\xed",
                hex(dtb_at))
    return facts


def gate_relocs(g: Gate, cross: str, elf: Path, syms: dict) -> dict:
    """POSITION_INDEPENDENT: start.S's pie_fixup applies only R_AARCH64_RELATIVE (1027) and skips anything else,
    so any other type would be a silent wrong pointer at a load address that is not TEXT_BASE."""
    out = run([f"{cross}readelf", "-rW", str(elf)])
    rows = [l.split() for l in out.splitlines() if re.match(r"^[0-9a-f]{8,16}\s", l)]
    types = {}
    for r in rows:
        types[r[2]] = types.get(r[2], 0) + 1
    start, bss_end = syms.get("_start", 0), syms.get("__bss_end", 0)
    bad = [r for r in rows if r[2] != "R_AARCH64_RELATIVE"
           or not start <= int(r[0], 16) < syms.get("_end", 0)
           or not start <= int(r[-1], 16) <= max(bss_end, syms.get("_end", 0)) + 0x100000]
    facts = {"count": len(rows), "types": types, "outside": [" ".join(r) for r in bad[:5]]}
    g.check("relocations: all R_AARCH64_RELATIVE, inside the image", rows and not bad, facts)
    return facts


def _pins(tree: fdt_tree.Tree, group: str) -> set:
    cells = tree.cells(group, "rockchip,pins") if "rockchip,pins" in tree.nodes[group] else []
    return {(cells[i], cells[i + 1]) for i in range(0, len(cells) - 3, 4)}


def gate_dt(g: Gate, dtb: bytes, family: str, binfile: Path, flavor: str = "chainload") -> dict:
    data = binfile.read_bytes()
    g.check("dt: u-boot.bin ends with u-boot.dtb", data.endswith(dtb), {"dtb_bytes": len(dtb)})
    tree = fdt_tree.Tree(dtb)
    nodes = tree.nodes

    def enabled(path):
        cur = path
        while True:
            st = nodes.get(cur, {}).get("status", b"okay\0").rstrip(b"\0")
            if st not in (b"okay", b"ok"):
                return False
            if cur == "/":
                return True
            cur = cur.rsplit("/", 1)[0] or "/"

    live = {p: props for p, props in nodes.items() if "compatible" in props and enabled(p)
            and p != "/" and not p.startswith("/binman")}
    allowed = DT_ALLOWED + FLAVOR[flavor].get("dt_allowed", ())
    odd = {p: tree.strings(p, "compatible") for p in live
           if not all(c in allowed for c in tree.strings(p, "compatible"))}
    g.check("dt: every enabled node is on the allow list", not odd, odd or sorted(live))
    compat = tree.strings("/", "compatible")
    g.check("dt: the family's root compatible", compat[:1] == [f"rigol,{family}"], compat)
    g.check("dt: console serial2 at 115200", tree.strings("/chosen", "stdout-path") == ["serial2:115200n8"],
            tree.strings("/chosen", "stdout-path") if "stdout-path" in nodes.get("/chosen", {}) else None)
    sd = next((p for p in nodes if p.endswith("@fe320000")), None)
    sdp = nodes.get(sd, {})
    aliases = nodes.get("/aliases", {})
    mmc0 = aliases.get("mmc0", b"").rstrip(b"\0").decode()
    facts = {"sdmmc": sd, "mmc0": mmc0, "max-frequency": tree.cells(sd, "max-frequency") if "max-frequency" in sdp else None,
             "bus-width": tree.cells(sd, "bus-width") if "bus-width" in sdp else None,
             "aliases": sorted(aliases)}
    g.check("dt: mmc0 = the SD host, enabled, 4-bit, <= 50 MHz, SD high speed, no supplies, no UHS",
            sd is not None and enabled(sd) and mmc0 == sd and facts["bus-width"] == [4]
            and facts["max-frequency"] and facts["max-frequency"][0] <= 50000000 and "cap-sd-highspeed" in sdp
            and not [p for p in SDMMC_FORBIDDEN_PROPS if p in sdp], facts)
    g.check("dt: no other mmc alias", [a for a in aliases if a.startswith("mmc")] == ["mmc0"], sorted(aliases))
    wdt = [p for p in nodes if p.endswith("@ff848000")]
    g.check("dt: the watchdog is disabled", wdt and not any(enabled(p) for p in wdt), wdt)
    gpios = {p: [k for k in props if k.endswith("gpios") or k.endswith("-gpio")] for p, props in live.items()}
    gpios = {p: v for p, v in gpios.items() if v}
    g.check("dt: no enabled node names a GPIO", not gpios, gpios or "none")
    assigned = [p for p, props in live.items() if "assigned-clocks" in props or "assigned-clock-rates" in props]
    g.check("dt: no enabled node reprograms a clock (assigned-clocks)", not assigned, assigned or "none")
    claimed = set()
    for p, props in live.items():
        for k in props:
            if k.startswith("pinctrl-") and k != "pinctrl-names":
                for h in tree.cells(p, k):
                    claimed |= _pins(tree, tree.handles[h])
    others = sorted(claimed - PINS_ALLOWED)
    never = {**NEVER[family], **NEVER["both"]}
    hit = {f"GPIO{b}_{'ABCD'[i // 8]}{i % 8}": never[(b, i)] for b, i in claimed if (b, i) in never}
    g.check("dt: pins claimed are UART2C, the SD bus and SD card-detect only", not others,
            {"claimed": sorted(claimed), "others": others})
    g.check(f"dt: none of the {family}'s never-drive pins", not hit, hit or sorted(never))
    return {"enabled": sorted(live), "pins": sorted(claimed)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--build", type=Path, required=True, help="the U-Boot O= directory")
    ap.add_argument("--family", choices=("dho900", "mho900"), required=True)
    ap.add_argument("--flavor", choices=tuple(FLAVOR), required=True)
    ap.add_argument("--cross", default="aarch64-linux-gnu-")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    g = Gate()
    cfg = read_config(a.build / ".config")
    gate_config(g, cfg, a.family, a.flavor)
    elf, binfile = a.build / "u-boot", a.build / "u-boot.bin"
    syms = gate_binary(g, a.cross, elf, binfile, a.flavor)
    report = {"family": a.family, "flavor": a.flavor, "u-boot.bin": hashlib.sha256(binfile.read_bytes()).hexdigest(),
              "header": gate_header(g, binfile, syms), "relocations": gate_relocs(g, a.cross, elf, syms),
              "dt": gate_dt(g, (a.build / "u-boot.dtb").read_bytes(), a.family, binfile, a.flavor)}
    data = binfile.read_bytes()
    g.check("env: silent=1 and the flavor's bootcmd in the default environment",
            b"silent=1\0" in data and f"bootcmd={FLAVOR[a.flavor]['bootcmd']}\0".encode() in data,
            FLAVOR[a.flavor]["bootcmd"])
    report["gates"] = g.results
    report["ok"] = g.ok
    text = json.dumps(report, indent=1)
    if a.json:
        a.json.write_text(text + "\n", encoding="utf-8", newline="\n")
    for r in g.results:
        print(("PASS " if r["ok"] else "FAIL ") + r["gate"] + ("" if r["ok"] else f": {json.dumps(r['detail'])[:400]}"))
    print(f"{a.family} {a.flavor}: {'PASS' if g.ok else 'FAIL'}")
    return 0 if g.ok else 1


if __name__ == "__main__":
    sys.exit(main())
