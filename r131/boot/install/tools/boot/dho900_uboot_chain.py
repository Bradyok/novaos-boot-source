#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""The chainload image of the Rigol DHO800/DHO900 and MHO900 (RK3399): the image the scope's stock U-Boot boots from
`recovery` with mainline U-Boot proper as its "kernel", and the LZ4 FIT that U-Boot proper then boots NovaOS from.
Writes no device.

    dho900_uboot_chain.py build --stage-b stage-b.img --uboot u-boot.bin --parm-cmdline parm-cmdline.txt \
        [--lz4 lz4] --out recovery-uboot-chain.img
    dho900_uboot_chain.py check recovery-uboot-chain.img [--stage-b stage-b.img] --parm-cmdline parm-cmdline.txt
    dho900_uboot_chain.py kernel-lz4 --stage-b stage-b.img [--lz4 lz4] --out Image.lz4

The U-Boot side is board/rigol/nova_rk3399 (`nova boot`, nova_rk3399.c); parm-cmdline.txt is the CMDLINE of the
card's partition table (PARM), which the stock U-Boot puts on every kernel command line.

THE CHAIN IMAGE, in `recovery` (the committed image) or the slot at `backup` + 32 MiB (a new image on trial, or the
committed one's copy, which U-Boot proper's boot ladder tries when the committed image does not come up):

    ANDROID! v0 header        what the stock U-Boot reads and checks (SHA1 id over the three sections below and
                              header bytes 32..575); its command-line field, which the stock U-Boot never reads,
                              holds `nova.fit=<bytes>:<crc32>` (fit_field), so the id names the FIT as well
    kernel  = u-boot.bin      the chainload flavor of U-Boot proper (an arm64 Image header, position
                              independent): the stock U-Boot loads it raw to 0x280000 and enters it at EL2
                              with x0 = the FDT it built
    ramdisk = a tiny cpio     the NovaOS boot image's (boot_ramdisk): the stock U-Boot needs one; nobody uses it
    second  = RSCE            the release's own `second` byte for byte: rk-kernel.dtb, the stage-B DTB. The stock
                              U-Boot builds the kernel command line from it exactly as it does today (DTB bootargs +
                              PARM CMDLINE + its own words) and passes it in x0, where U-Boot proper takes it
    FIT                       at the next page after `second` (the stock U-Boot never reads it):
        /images/kernel        the release's raw Image as one LZ4 frame (`lz4 -9`, the format U-Boot's ulz4fn takes:
                              version 1, independent blocks), load = entry = 0x200000
        /images/fdt           the release's DTB, byte for byte
        /images/ramdisk       the release's stage-B initramfs, byte for byte, load 0x04bf0000 (the stock address)
        /configurations/conf-1  kernel, fdt, ramdisk, and nova,bootargs: the line the stock U-Boot builds, computed
                              here the same way (uboot_cmdline), U-Boot proper's fallback when x0 has no FDT
      every image carries a crc32 hash (bootm checks it), and U-Boot proper holds the whole FIT to the header's
      length and crc32 before it boots it: a torn or corrupt FIT is the next rung of its ladder.
    zeros                     to the next 2 KiB page: the image is whole pages, like every NovaOS boot image

Memory on the unit (stock BL31 and OP-TEE resident, OP-TEE at 0x08400000-0x0a1fffff): the kernel at 0x200000
up to its image_size, the ramdisk at 0x04bf0000, both below the OP-TEE hole; the FIT read to 0x20000000
(fit_addr_r in board/rigol/nova_rk3399/nova_rk3399.env), below the stock logo pool at 0xf5c00000.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "kernel")]
import rk_bootimg as RB  # noqa: E402  the boot image format
import tu15_fdt  # noqa: E402

TrialError = RB.BootImageError

# U-Boot proper's numbers (board/rigol/nova_rk3399: Kconfig, nova_rk3399.env); check_chain holds the image to them.
KERNEL_LOAD = 0x00200000          # FIT kernel load = entry: 2 MiB aligned, text_offset 0 (booti_setup moves nothing)
RAMDISK_LOAD = RB.RAMDISK_ADDR    # 0x04bf0000, where the stock U-Boot puts it
TRUST_OS = (0x08400000, 0x01E00000)   # stock OP-TEE, resident (NOVA_TRUST_OS_BASE / _SIZE)
FIT_ADDR = 0x20000000             # fit_addr_r
FIT_MAX = 0x4000000               # NOVA_FIT_MAX
FIT_CONFIG = "conf-1"             # NOVA_FIT_CONFIG
LOGO_POOL = 0xF5C00000            # the stock U-Boot's logo pool
UBOOT_VERSION = b"U-Boot 2026.10"  # the pinned U-Boot release
SECTOR = 512
# What U-Boot proper appends to the line (nova_chain.h nova_add_words): " nova.chain=<role> nova.chain.slot=<rung>
# nova.chain.attempt=<n>/<of>", at most 69 bytes; held free under the 2048-byte line.
CHAIN_WORDS_MAX = 80
FDT_MAGIC = struct.pack(">I", 0xD00DFEED)

# ---------------------------------------------------------------------------------------------- LZ4

LZ4_MAGIC = 0x184D2204


def lz4_frame(data: bytes) -> dict:
    """The LZ4 frame header, held to what U-Boot's ulz4fn (lib/lz4_wrapper.c) accepts: one frame, version 1,
    independent blocks, no dictionary id, reserved bits zero. The legacy format `make Image.lz4` writes (lz4 -l,
    magic 02 21 4c 18) is refused there, so it is refused here."""
    if len(data) < 7:
        raise TrialError("lz4: shorter than a frame header")
    magic, flg, bd = struct.unpack_from("<IBB", data)
    if magic != LZ4_MAGIC:
        raise TrialError(f"lz4: magic {magic:#x}, not the frame format's {LZ4_MAGIC:#x} (U-Boot refuses lz4 -l)")
    if flg >> 6 != 1:
        raise TrialError(f"lz4: frame version {flg >> 6}")
    if flg & 0x03 or bd & 0x8F:
        raise TrialError(f"lz4: reserved bits or a dictionary id set (FLG {flg:#x} BD {bd:#x}): ulz4fn refuses them")
    if not flg & 0x20:
        raise TrialError("lz4: linked blocks; ulz4fn takes independent blocks only")
    return {"block_checksum": bool(flg & 0x10), "content_size": bool(flg & 0x08),
            "content_checksum": bool(flg & 0x04), "block_max": 1 << (8 + 2 * ((bd >> 4) & 7))}


def _lz4_block(src: bytes) -> bytes:
    """One LZ4 block (the block format), decoded on its own: a match may only reach back into this block."""
    out = bytearray()
    i, n = 0, len(src)
    while True:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            if i > n:
                raise TrialError("lz4: literals run past the block")
            return bytes(out)
        off = src[i] | src[i + 1] << 8
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off == 0 or start < 0:
            raise TrialError(f"lz4: match offset {off} outside the block's output")
        if off >= ml:
            out += out[start:start + ml]
        else:
            chunk = bytes(out[start:])
            out += (chunk * (ml // off + 1))[:ml]


def lz4_decompress(data: bytes) -> bytes:
    """A whole frame, decoded the way ulz4fn walks it (blocks until the zero end mark), plus the checks it skips:
    nothing after the end mark but the content checksum."""
    info = lz4_frame(data)
    pos = 6 + (8 if info["content_size"] else 0) + 1
    out = bytearray()
    while True:
        if pos + 4 > len(data):
            raise TrialError("lz4: no end mark")
        bh = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if bh == 0:
            break
        size = bh & 0x7FFFFFFF
        if pos + size > len(data):
            raise TrialError("lz4: a block runs past the frame")
        block = data[pos:pos + size]
        pos += size
        out += block if bh & 0x80000000 else _lz4_block(block)
        if info["block_checksum"]:
            pos += 4
    if info["content_checksum"]:
        pos += 4
    if pos != len(data):
        raise TrialError(f"lz4: {len(data) - pos} bytes after the frame")
    return bytes(out)


def lz4_tool_version(lz4: str) -> str:
    r = subprocess.run([lz4, "--version"], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "unknown"


def lz4_compress(data: bytes, lz4: str = "lz4") -> bytes:
    """`lz4 -9` on the data, in files (not a pipe: the same frame whatever lz4 does with a non-seekable output),
    then decoded back here and compared."""
    if not shutil.which(lz4):
        raise TrialError(f"lz4: no `{lz4}` (build the pinned lz4 release)")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "Image", Path(tmp) / "Image.lz4"
        src.write_bytes(data)
        subprocess.run([lz4, "-q", "-9", "-f", str(src), str(dst)], check=True)
        out = dst.read_bytes()
    if lz4_decompress(out) != data:
        raise TrialError("lz4: the frame does not decode to the Image it was made from")
    return out


# ---------------------------------------------------------------------------------------------- the FIT

def _image(w, node: str, data: bytes, *, type_: str, description: str, compression: str = "none",
           load: int | None = None, entry: int | None = None, os_: str | None = None) -> None:
    w.begin(node)
    w.prop_str("description", description)
    w.prop("data", data)
    w.prop_str("type", type_)
    w.prop_str("arch", "arm64")
    if os_:
        w.prop_str("os", os_)
    w.prop_str("compression", compression)
    if load is not None:
        w.prop_u32("load", load)
    if entry is not None:
        w.prop_u32("entry", entry)
    w.begin("hash-1").prop_str("algo", "crc32").prop("value", struct.pack(">I", zlib.crc32(data))).end()
    w.end()


def build_fit(image_lz4: bytes, dtb: bytes, ramdisk: bytes, bootargs: str, board: str) -> bytes:
    """The FIT U-Boot proper boots (`bootm <fit_addr_r>#conf-1`). timestamp 0: the same inputs give the same bytes."""
    w = tu15_fdt.Writer()
    w.begin("")
    w.prop_str("description", f"NovaOS {board}: the stage-B kernel (LZ4), DTB and initramfs for mainline U-Boot proper")
    w.prop_u32("timestamp", 0)
    w.prop_u32("#address-cells", 1)
    w.begin("images")
    _image(w, "kernel", image_lz4, type_="kernel", os_="linux", compression="lz4", load=KERNEL_LOAD, entry=KERNEL_LOAD,
           description="Linux arm64 Image (lz4 -9 frame)")
    _image(w, "fdt", dtb, type_="flat_dt", description=f"rk3399-rigol-{board}.dtb (the stage-B DTB)")
    _image(w, "ramdisk", ramdisk, type_="ramdisk", os_="linux", load=RAMDISK_LOAD,
           description="the stage-B initramfs (gzip cpio)")
    w.end()
    w.begin("configurations")
    w.prop_str("default", FIT_CONFIG)
    w.begin(FIT_CONFIG)
    w.prop_str("description", f"NovaOS stage B on the {board}")
    w.prop_str("kernel", "kernel")
    w.prop_str("fdt", "fdt")
    w.prop_str("ramdisk", "ramdisk")
    w.prop_str("nova,bootargs", bootargs)
    w.end()
    w.end()
    w.end()
    return w.to_bytes()


def check_fit(fit: bytes) -> dict:
    """Read a FIT back as U-Boot proper's bootm will: the configuration, the three images, their hashes, the
    kernel's frame and Image header, and where everything lands. Returns the payloads and the facts."""
    try:
        tree = tu15_fdt.Tree(fit)
    except ValueError as e:
        raise TrialError(f"FIT: {e}") from None
    if len(fit) > FIT_MAX:
        raise TrialError(f"FIT: {len(fit)} bytes, over U-Boot proper's NOVA_FIT_MAX {FIT_MAX:#x}")
    if FIT_ADDR + len(fit) > LOGO_POOL:
        raise TrialError("FIT: at fit_addr_r it would reach the stock logo pool")
    nodes = tree.nodes
    conf = f"/configurations/{FIT_CONFIG}"
    if tree.strings("/configurations", "default") != [FIT_CONFIG] or conf not in nodes:
        raise TrialError(f"FIT: default configuration is not {FIT_CONFIG}")
    want = {"kernel": "kernel", "fdt": "fdt", "ramdisk": "ramdisk"}
    for prop, image in want.items():
        if tree.strings(conf, prop) != [image]:
            raise TrialError(f"FIT: {conf} {prop} is not {image}")
    out = {}
    for image in want.values():
        path = f"/images/{image}"
        props = nodes.get(path)
        if props is None or "data" not in props:
            raise TrialError(f"FIT: no {path} data")
        data = props["data"]
        if tree.strings(path, "arch") != ["arm64"]:
            raise TrialError(f"FIT: {path} arch is not arm64")
        h = nodes.get(f"{path}/hash-1", {})
        if h.get("algo") != b"crc32\0" or h.get("value") != struct.pack(">I", zlib.crc32(data)):
            raise TrialError(f"FIT: {path} hash-1 is not the crc32 of its data")
        out[image] = data
    k = "/images/kernel"
    if (tree.strings(k, "type"), tree.strings(k, "os"), tree.strings(k, "compression")) != (["kernel"], ["linux"], ["lz4"]):
        raise TrialError("FIT: /images/kernel is not an LZ4 Linux kernel")
    if tree.cells(k, "load") != [KERNEL_LOAD] or tree.cells(k, "entry") != [KERNEL_LOAD]:
        raise TrialError(f"FIT: kernel load/entry are not {KERNEL_LOAD:#x}")
    frame = lz4_frame(out["kernel"])
    image = lz4_decompress(out["kernel"])
    if image[0x38:0x3C] != RB.ARM64_MAGIC:
        raise TrialError("FIT: the decompressed kernel has no arm64 Image magic")
    text_offset, image_size, _ = struct.unpack_from("<QQQ", image, 8)
    if text_offset:
        raise TrialError(f"FIT: kernel text_offset {text_offset:#x}")
    kernel_end = KERNEL_LOAD + max(image_size, len(image))
    if kernel_end > RAMDISK_LOAD:
        raise TrialError(f"FIT: the kernel at {KERNEL_LOAD:#x} ends at {kernel_end:#x}, over the ramdisk at {RAMDISK_LOAD:#x}")
    if tree.strings("/images/fdt", "type") != ["flat_dt"] or tree.strings("/images/fdt", "compression") != ["none"]:
        raise TrialError("FIT: /images/fdt is not an uncompressed flat_dt")
    r = "/images/ramdisk"
    if (tree.strings(r, "type"), tree.strings(r, "compression")) != (["ramdisk"], ["none"]):
        raise TrialError("FIT: /images/ramdisk is not an uncompressed ramdisk")
    if tree.cells(r, "load") != [RAMDISK_LOAD]:
        raise TrialError(f"FIT: ramdisk load is not {RAMDISK_LOAD:#x}")
    ramdisk_end = RAMDISK_LOAD + len(out["ramdisk"])
    if ramdisk_end > TRUST_OS[0]:
        raise TrialError(f"FIT: the ramdisk ends at {ramdisk_end:#x}, in the OP-TEE region at {TRUST_OS[0]:#x}")
    bootargs = nodes[conf].get("nova,bootargs", b"")
    if not bootargs.endswith(b"\0") or len(bootargs) > RB.UBOOT_CMDLINE_LIMIT:
        raise TrialError("FIT: nova,bootargs absent, unterminated or over the stock U-Boot's 0x800 bytes")
    return {"image": image, "fdt": out["fdt"], "ramdisk": out["ramdisk"], "bootargs": bootargs[:-1].decode(),
            "facts": {"bytes": len(fit), "kernel_lz4_bytes": len(out["kernel"]), "kernel_bytes": len(image),
                      "lz4_ratio": round(len(out["kernel"]) / len(image), 4), "lz4_frame": frame,
                      "kernel_image_size": hex(image_size), "kernel_end": hex(kernel_end),
                      "ramdisk_end": hex(ramdisk_end), "fit_at": hex(FIT_ADDR),
                      "sha256": hashlib.sha256(fit).hexdigest()}}


# ---------------------------------------------------------------------------------------------- U-Boot proper

def uboot_facts(uboot: bytes, board: str | None = None) -> dict:
    """The chainload u-boot.bin as the stock U-Boot will take it (check_kernel: arm64 header, text_offset 0, inside
    its reservation) and as the U-Boot build made it: the pinned version, the chainload flavor, its control
    DT (appended last) naming this board."""
    kernel = RB.check_kernel(uboot)
    if UBOOT_VERSION not in uboot:
        raise TrialError(f"u-boot.bin: no {UBOOT_VERSION.decode()!r} banner: not the pinned U-Boot")
    if b"bootcmd=nova boot\0" not in uboot:
        raise TrialError("u-boot.bin: its default bootcmd is not `nova boot`: not the chainload flavor "
                         "(board/rigol/nova_rk3399/nova-chainload.config)")
    # The DTB ends the file, but for the zeros pad_uboot() adds (fewer than 4).
    at = uboot.rfind(FDT_MAGIC)
    while at > 0 and not (0 <= len(uboot) - at - struct.unpack_from(">I", uboot, at + 4)[0] < 4
                          and not uboot[at + struct.unpack_from(">I", uboot, at + 4)[0]:].strip(b"\0")):
        at = uboot.rfind(FDT_MAGIC, 0, at)
    if at <= 0:
        raise TrialError("u-boot.bin: no control DT at its end (OF_SEPARATE)")
    dt = tu15_fdt.Tree(uboot[at:at + struct.unpack_from(">I", uboot, at + 4)[0]])
    compat = dt.strings("/", "compatible")
    if board and f"rigol,{board}" not in compat:
        raise TrialError(f"u-boot.bin: control DT {compat} is not the {board}'s: one build per family")
    return {"bytes": len(uboot), "image_size": kernel["image_size"], "control_dt": compat,
            "sha256": hashlib.sha256(uboot).hexdigest()}


def pad_uboot(uboot: bytes) -> bytes:
    """u-boot.bin with zeros to a whole number of words: the stock U-Boot hashes whole words (assemble refuses
    anything else). The zeros follow the control DT, which U-Boot proper finds at _end, so they change nothing."""
    return uboot + bytes(-len(uboot) % 4)


# ---------------------------------------------------------------------------------------------- the chain image

CHAIN_KINDS = ("stage-b", "debian")


def release_parts(stage_b: bytes, gate=None, kind: str = "stage-b") -> dict:
    """The release's stage-B image (or, kind `debian`, the Debian boot image: build-dho900-recovery-trial.py
    build-debian), held to `gate` first (the build's own image gate, when it has one), taken apart."""
    if kind not in CHAIN_KINDS:
        raise TrialError(f"chain kind {kind!r}: {' or '.join(CHAIN_KINDS)}")
    if gate is not None:
        gate(stage_b, kind)
    kernel, ramdisk, second = RB.image_parts(stage_b)
    dtb = RB.rsce_payload(second, "rk-kernel.dtb")
    dt = RB.dtb_facts(dtb)
    return {"kernel": kernel, "ramdisk": ramdisk, "second": second, "dtb": dtb, "dt": dt,
            "sha256": hashlib.sha256(stage_b).hexdigest()}


def kernel_lz4(stage_b: bytes, lz4: str = "lz4", gate=None) -> tuple[bytes, dict]:
    """The release kernel as the FIT carries it: the kexec test's LZ4 input (kexec -s --initrd=Image.lz4)."""
    parts = release_parts(stage_b, gate)
    out = lz4_compress(parts["kernel"], lz4)
    return out, {"board": parts["dt"]["board"], "kernel_bytes": len(parts["kernel"]),
                 "kernel_crc32": f"{zlib.crc32(parts['kernel']):08x}",
                 "kernel_sha256": hashlib.sha256(parts["kernel"]).hexdigest(), "lz4_bytes": len(out),
                 "lz4_sha256": hashlib.sha256(out).hexdigest(), "lz4_tool": lz4_tool_version(lz4)}


def fit_field(fit: bytes) -> bytes:
    """The ANDROID! header's command-line field of a chain image: the FIT's length and crc32. The stock U-Boot never
    reads the field, but its SHA1 id covers it, so the id names the FIT too: U-Boot proper holds the FIT it reads to
    these two numbers (nova_chain.h nova_fit_field), and two chain images with the same id are the same image."""
    return f"nova.fit={len(fit)}:{zlib.crc32(fit):08x}".encode()


def parse_fit_field(header: bytes) -> tuple[int, int] | None:
    """(bytes, crc32) from a chain image's header command-line field, or None (not a chain image of ours)."""
    field = header[64:576].split(b"\0", 1)[0].decode("ascii", "replace")
    for word in field.split(" "):
        if word.startswith("nova.fit="):
            n, _, crc = word[len("nova.fit="):].partition(":")
            if n.isdigit() and len(crc) == 8 and all(c in "0123456789abcdef" for c in crc):
                return int(n), int(crc, 16)
    return None


# What the release's stage-B initramfs must carry for the chain's boot ladder (U-Boot proper's attempts at the committed
# image, its copy and a trial): an init that reads the chain's words and clears its attempt record (a fallback boot goes
# on with its own root, a trial with nothing on trial never reaches one), and the bootsel whose verbs write the slot and
# the record.
CHAIN_INIT_MARKS = (b"nova.chain=", b"nova.chain.slot=", b"chain_clear", b"slot_md5")
CHAIN_BOOTSEL_MARKS = (b"backup:slot) node=$BACKUP ;;", b"recovery:promote) node=$RECOVERY ;;", b"try-clear)",
                       b"try-arm|try-arm-recovery)")


def newc_file(archive: bytes, want: str) -> bytes | None:
    """Regular file `want` of a newc cpio archive, or None."""
    pos = 0
    while pos + 110 <= len(archive) and archive[pos:pos + 6] in (b"070701", b"070702"):
        f = [int(archive[pos + 6 + 8 * i:pos + 14 + 8 * i], 16) for i in range(13)]
        name = archive[pos + 110:pos + 110 + f[11] - 1].decode(errors="replace")
        body = (pos + 110 + f[11] + 3) & ~3
        if name == "TRAILER!!!":
            return None
        if name == want and f[1] & 0o170000 == 0o100000:
            return archive[body:body + f[6]]
        pos = (body + f[6] + 3) & ~3
    return None


def chain_ready(ramdisk: bytes) -> dict:
    """The release's stage-B initramfs (gzip newc) holds an init and a bootsel that take part in the chain's ladder;
    TrialError otherwise: an older release in a chain image would boot a trial's kernel on its committed root."""
    import gzip
    try:
        archive = gzip.decompress(ramdisk)
    except OSError as e:
        raise TrialError(f"the stage-B initramfs is not gzip: {e}") from None
    init, bootsel = newc_file(archive, "init"), newc_file(archive, "bin/nova-dho900-bootsel")
    missing = [m.decode() for m in CHAIN_INIT_MARKS if init is None or m not in init]
    missing += [m.decode() for m in CHAIN_BOOTSEL_MARKS if bootsel is None or m not in bootsel]
    if missing:
        raise TrialError(f"the stage-B initramfs does not take part in the chain (its init / bootsel lack {missing}): "
                         "a release from before Step 1's ladder cannot go into a chain image")
    return {"init_sha256": hashlib.sha256(init).hexdigest(), "bootsel_sha256": hashlib.sha256(bootsel).hexdigest()}


def chain_budget(bootargs: str, parm_cmdline: str) -> dict:
    """The line the stock U-Boot builds for `recovery`, with room for the words U-Boot proper adds (CHAIN_WORDS_MAX:
    the role, slot and attempt, nova_chain.h nova_add_words), held to the kernel's 2048 bytes with its NUL."""
    line = RB.uboot_cmdline(bootargs, parm_cmdline, recovery_boot=True)
    n = len(line.encode()) + CHAIN_WORDS_MAX + 1
    if n > RB.UBOOT_CMDLINE_LIMIT:
        raise TrialError(f"the kernel command line with the chain's words would be {n} bytes; it holds "
                         f"{RB.UBOOT_CMDLINE_LIMIT:#x}")
    return {"kernel_cmdline": line, "bytes_with_words": n, "limit": RB.UBOOT_CMDLINE_LIMIT}


def build_chain(stage_b: bytes, uboot: bytes, parm_cmdline: str, lz4: str = "lz4", gate=None,
                kind: str = "stage-b") -> tuple[bytes, dict]:
    """The chain image: u-boot.bin as the kernel of an ANDROID! image whose `second` is the release's and whose header
    names the FIT (fit_field), and the FIT of the release's kernel (LZ4), DTB and initramfs after it, padded to whole
    2 KiB pages like every NovaOS boot image (the update tools copy and compare images by pages).
    parm_cmdline: the PARM's CMDLINE.
    kind `debian`: the image is the Debian boot image (the bare edition: plain Debian 13 on the scope's stock /data),
    whose initramfs takes no part in the ladder (chain_ready is NovaOS's): its root confirms a boot that stayed up by
    clearing the attempt record itself (the bare edition's rigol-boot-ok.timer), so a chain image of it falls back as
    NovaOS's does only on a boot that never got that far."""
    parts = release_parts(stage_b, gate, kind)
    dt = parts["dt"]
    board = dt["board"] or "dho900"
    RB.cmdline_budget(dt["bootargs"], parm_cmdline)
    chain_budget(dt["bootargs"], parm_cmdline)
    ready = chain_ready(parts["ramdisk"]) if kind == "stage-b" else {
        "kind": kind, "ladder": "the root clears the attempt record 180 s after boot (rigol-boot-ok.timer)",
        "ramdisk_sha256": hashlib.sha256(parts["ramdisk"]).hexdigest()}
    bootargs = RB.uboot_cmdline(dt["bootargs"], parm_cmdline, recovery_boot=True)
    image_lz4 = lz4_compress(parts["kernel"], lz4)
    fit = build_fit(image_lz4, parts["dtb"], parts["ramdisk"], bootargs, board)
    uboot = pad_uboot(uboot)
    uboot_facts(uboot, board)
    wrapper, _ = RB.assemble(uboot, parts["dtb"], RB.boot_ramdisk(), cmdline=fit_field(fit))
    blob = wrapper + fit + bytes(-len(fit) % RB.PAGE)
    receipt = check_chain(blob, parm_cmdline, stage_b)
    receipt["lz4_tool"] = lz4_tool_version(lz4)
    receipt["chain_ready"] = ready
    receipt["kind"] = kind
    return blob, receipt


def check_chain(blob: bytes, parm_cmdline: str, stage_b: bytes | None = None) -> dict:
    """Read a chain image back the way the stock U-Boot and then U-Boot proper will, and, given the release's
    stage-B image, hold the FIT's kernel, DTB and ramdisk to it byte for byte."""
    return _check_chain(blob, parm_cmdline, stage_b)[0]


def chain_parts(blob: bytes, parm_cmdline: str) -> dict:
    """What a chain image carries of the release it was made from, once check_chain holds: the kernel (the FIT's LZ4
    frame decoded), the DTB, the initramfs and the image's `second`, with the receipt. The release's own boot image is
    these put together again (rk_bootimg.assemble of kernel, DTB and initramfs is deterministic, and its `second` is
    this one), which is how the tools that read a stage-B image read a chain image."""
    receipt, fit = _check_chain(blob, parm_cmdline, None)
    return {"kernel": fit["image"], "dtb": fit["fdt"], "ramdisk": fit["ramdisk"], "second": RB.image_parts(blob)[2],
            "receipt": receipt}


def is_chain(blob: bytes) -> bool:
    """Whether a boot image is a chain image of ours (its header's command-line field names its FIT)."""
    return blob[:8] == b"ANDROID!" and parse_fit_field(blob[:RB.PAGE]) is not None


def _check_chain(blob: bytes, parm_cmdline: str, stage_b: bytes | None) -> tuple[dict, dict]:
    if blob[:8] != b"ANDROID!":
        raise TrialError("no ANDROID! magic")
    ks, _, rs, _, ss, _, _, page = struct.unpack_from("<8I", blob, 8)
    if page != RB.PAGE:
        raise TrialError(f"page_size {page}")
    if blob[0x400:0x404] == b"SIGN":
        raise TrialError("hdr+0x400 holds SIGN: the stock U-Boot would try an RSA check")
    uboot, ramdisk, second = RB.image_parts(blob)
    if len(second) != ss or not ss:
        raise TrialError("second truncated or absent")
    if RB.boot_image_id(blob[:RB.PAGE], uboot, ramdisk, second) != blob[576:608]:
        raise TrialError("id does not match the Rockchip SHA1 of the image")
    if any(n % 4 for n in (ks, rs, ss)):
        raise TrialError(f"section sizes {ks}/{rs}/{ss}: the stock U-Boot's hash engine reads whole words")
    if (rs + 511) // 512 * 512 + ss > RB.RAMDISK_SECOND_LIMIT:
        raise TrialError("ramdisk + second over the stock U-Boot's buffer")
    if len(blob) > RB.RECOVERY_BYTES:
        raise TrialError(f"image is {len(blob)} bytes; `recovery` is {RB.RECOVERY_BYTES}")
    if [n for n, _, _ in RB.rsce_parse(second)] != ["rk-kernel.dtb"]:
        raise TrialError("second: want exactly one RSCE entry, rk-kernel.dtb")
    dtb = RB.rsce_payload(second, "rk-kernel.dtb")
    dt = RB.dtb_facts(dtb)
    board = dt["board"] or "dho900"
    if blob[48:64].split(b"\0", 1)[0] != RB.BOARD_NAMES[dt["board"]]:
        raise TrialError("header name is not the DTB's board's")
    ub = uboot_facts(uboot, board)
    fit_at = page + sum((n + page - 1) // page * page for n in (ks, rs, ss))
    if fit_at % SECTOR:
        raise TrialError("the FIT does not start on a sector")
    fit_len = struct.unpack_from(">I", blob, fit_at + 4)[0] if blob[fit_at:fit_at + 4] == FDT_MAGIC else 0
    if not fit_len:
        raise TrialError(f"no FIT at {fit_at:#x} (after `second`)")
    if fit_at + fit_len > len(blob) or blob[fit_at + fit_len:].strip(b"\0"):
        raise TrialError("the FIT is cut short, or something follows it")
    if len(blob) % RB.PAGE:
        raise TrialError(f"the image is {len(blob)} bytes, not whole {RB.PAGE}-byte pages")
    named = parse_fit_field(blob[:RB.PAGE])
    if named != (fit_len, zlib.crc32(blob[fit_at:fit_at + fit_len])):
        raise TrialError(f"the header's nova.fit= field names {named}, not the FIT's length and crc32 "
                         f"({fit_len}, {zlib.crc32(blob[fit_at:fit_at + fit_len]):08x})")
    fit = check_fit(blob[fit_at:fit_at + fit_len])
    if fit["fdt"] != dtb:
        raise TrialError("FIT /images/fdt is not the DTB the stock U-Boot reads from `second`")
    line = RB.uboot_cmdline(dt["bootargs"], parm_cmdline, recovery_boot=True)
    if fit["bootargs"] != line:
        raise TrialError("FIT nova,bootargs is not the line the stock U-Boot builds from this DTB")
    receipt = {"board": board, "bytes": len(blob), "uboot": ub, "fit_offset": fit_at, "fit": fit["facts"],
               "kernel_cmdline": line, "kernel_cmdline_bytes": len(line.encode()) + 1,
               "kernel_sha256": hashlib.sha256(fit["image"]).hexdigest(),
               "kernel_crc32": f"{zlib.crc32(fit['image']):08x}",
               "dtb_sha256": hashlib.sha256(dtb).hexdigest(),
               "ramdisk_sha256": hashlib.sha256(fit["ramdisk"]).hexdigest(),
               "sha256": hashlib.sha256(blob).hexdigest()}
    if stage_b is not None:
        parts = release_parts(stage_b)
        same = {"kernel": fit["image"] == parts["kernel"], "dtb": dtb == parts["dtb"],
                "ramdisk": fit["ramdisk"] == parts["ramdisk"], "second": second == parts["second"]}
        if not all(same.values()):
            raise TrialError(f"not byte-identical to the release's stage-B image: {same}")
        receipt["release"] = {"stage_b_sha256": parts["sha256"], "byte_identical": same}
    return receipt, fit


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="the chain image from a stage-B image and the chainload u-boot.bin")
    b.add_argument("--stage-b", type=Path, required=True)
    b.add_argument("--uboot", type=Path, required=True)
    b.add_argument("--parm-cmdline", type=Path, required=True, help="a file holding the PARM's CMDLINE")
    b.add_argument("--lz4", default="lz4")
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check", help="read a chain image back; with --stage-b, hold it to that image byte for byte")
    c.add_argument("image", type=Path)
    c.add_argument("--stage-b", type=Path)
    c.add_argument("--parm-cmdline", type=Path, required=True)
    k = sub.add_parser("kernel-lz4", help="the stage-B kernel as the FIT carries it")
    k.add_argument("--stage-b", type=Path, required=True)
    k.add_argument("--lz4", default="lz4")
    k.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            blob, receipt = build_chain(a.stage_b.read_bytes(), a.uboot.read_bytes(),
                                        a.parm_cmdline.read_text(encoding="utf-8").strip(), a.lz4)
            a.out.write_bytes(blob)
        elif a.cmd == "check":
            receipt = check_chain(a.image.read_bytes(), a.parm_cmdline.read_text(encoding="utf-8").strip(),
                                  a.stage_b.read_bytes() if a.stage_b else None)
        else:
            blob, receipt = kernel_lz4(a.stage_b.read_bytes(), a.lz4)
            a.out.write_bytes(blob)
    except TrialError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
