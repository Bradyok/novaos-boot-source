#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""The boot image format of the Rigol DHO800/DHO900 and MHO900's stock boot loader (Rockchip RK3399), as NovaOS
writes it: an Android v0 image (`ANDROID!`) whose `second` is a Rockchip resource container (`RSCE`) holding the
kernel's device tree, with Rockchip's image id in the header.

    the header page       magic, the three section sizes and load addresses, page size 2048, name[16],
                          cmdline[512], id[32]: the SHA1 the loader checks (boot_image_id)
    kernel, ramdisk, second, each from a page boundary

The loader loads the kernel to KERNEL_ADDR and the ramdisk to RAMDISK_ADDR, takes `rk-kernel.dtb` from the RSCE in
`second`, and builds the kernel command line itself: the DTB's /chosen/bootargs, the command line of the card's
partition table (PARM), and its own words (uboot_cmdline).

No device is touched here: this module only reads and writes bytes.

Installing a kernel, device tree or initramfs you have changed (the commands below, INSTALLING.txt beside the
source has the whole road):

    python3 rk_bootimg.py show IMAGE                       its sections, board, command line and whether its id holds
    python3 rk_bootimg.py unpack IMAGE DIR                 kernel, ramdisk, rk-kernel.dtb and cmdline.txt into DIR
    python3 rk_bootimg.py repack IMAGE --out NEW [--kernel Image] [--dtb DTB] [--ramdisk FILE]
                                                           IMAGE with the parts given replaced, its id recomputed

`repack` keeps every part not given as IMAGE has it and the header's command-line field, so a repack with nothing
replaced gives IMAGE back byte for byte. A new DTB must carry the /chosen/bootargs of the one it replaces (the loader
builds the kernel's command line from it); copy it with dtc's fdtput:
    fdtput -t s new.dtb /chosen bootargs "$(fdtget old/rk-kernel.dtb /chosen bootargs)"
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kernel"))
import tu15_fdt  # noqa: E402


class BootImageError(Exception):
    pass


PAGE = 2048
KERNEL_ADDR = 0x00280000                # where the loader puts the kernel, and enters it
RAMDISK_ADDR = 0x04BF0000               # where it puts the ramdisk (the kernel's /chosen/linux,initrd-start)
KERNEL_LIMIT = 0x00200000 + 0x3800000   # the end of the loader's kernel reservation
RAMDISK_SECOND_LIMIT = 0x3000000        # ramdisk + second together, from RAMDISK_ADDR
RECOVERY_BYTES = 0x20000 * 512          # the `recovery` partition: 0x20000 sectors
ARM64_MAGIC = b"ARM\x64"
NAME = b"nova-dho900"                   # the header's name[16]
# The board an image is for: its DTB's root compatible; the header carries its name too.
BOARDS = {"rigol,dho900": "dho900", "rigol,mho900": "mho900"}
BOARD_NAMES = {None: NAME, "dho900": NAME, "mho900": b"nova-mho900"}
# The words the loader appends to every kernel command line, and what it puts before the partition list of a
# `recovery` / `backup` boot; the whole line fits its 0x800-byte buffer.
UBOOT_SUFFIX = ("storagemedia=sd androidboot.oem_unlocked=0 uboot_logo=0x02000000@0xf5c00000 "
                "loader.timestamp=2023-11-16_11:07:09 SecureBootCheckOk=0")
RECOVERY_MTDPARTS_PREFIX = "0x00002000@0x00000000(parameter),"
UBOOT_CMDLINE_LIMIT = 0x800
BOOT_ID_OFFSET = 8 + 10 * 4 + 16 + 512      # 576: magic[8], ten u32, name[16], cmdline[512], then id[32]


def pad(data: bytes) -> bytes:
    return data + bytes(-len(data) % PAGE)


def boot_image_id(header: bytes, kernel: bytes, ramdisk: bytes, second: bytes) -> bytes:
    """The id the loader checks before it boots an image (it refuses one whose id differs): SHA1 over the kernel and
    its le32 size, the ramdisk and its size, `second` and its size, then header bytes 32-575 (tags_addr, page_size,
    header_version, os_version, name, cmdline); 20 bytes, zero-padded to the 32-byte id field."""
    digest = hashlib.sha1()
    for payload in (kernel, ramdisk, second):
        digest.update(payload)
        digest.update(struct.pack("<I", len(payload)))
    digest.update(header[32:BOOT_ID_OFFSET])
    return digest.digest().ljust(32, b"\0")


# ---------------------------------------------------------------------------------------------- RSCE

RSCE_MAGIC = b"RSCE"
RSCE_ENTRY = b"ENTR"
RSCE_BLOCK = 512
RSCE_NAME_LEN = 256
# Block 0: magic, two u16 versions (0), header blocks (1), the table's first block (1), blocks per entry (1), a pad
# byte, the u32 entry count. Then one block per entry: "ENTR", name[256], the payload's first block, its size in
# bytes. Then the payloads, each from a block boundary, in table order.


def blocks_for(n: int) -> int:
    return (n + RSCE_BLOCK - 1) // RSCE_BLOCK


def rsce_build(entries: list[tuple[str, bytes]]) -> bytes:
    """An RSCE holding [(name, payload)], in that order."""
    if not entries:
        raise BootImageError("a resource image with no entries would give the loader nothing to find")
    for name, _ in entries:
        if len(name.encode()) >= RSCE_NAME_LEN:
            raise BootImageError(f"entry name {name!r} does not fit in the {RSCE_NAME_LEN}-byte name field")
    tbl_offset, entry_blocks = 1, 1
    header = bytearray(RSCE_BLOCK)
    header[0:4] = RSCE_MAGIC
    struct.pack_into("<HH", header, 4, 0, 0)
    header[8], header[9], header[10], header[11] = 1, tbl_offset, entry_blocks, 0
    struct.pack_into("<I", header, 12, len(entries))
    table, body = bytearray(), bytearray()
    cursor = tbl_offset + len(entries) * entry_blocks
    for name, payload in entries:
        row = bytearray(entry_blocks * RSCE_BLOCK)
        row[0:4] = RSCE_ENTRY
        encoded = name.encode()
        row[4:4 + len(encoded)] = encoded
        struct.pack_into("<II", row, 4 + RSCE_NAME_LEN, cursor, len(payload))
        table += row
        body += payload + bytes(blocks_for(len(payload)) * RSCE_BLOCK - len(payload))
        cursor += blocks_for(len(payload))
    return bytes(header) + bytes(table) + bytes(body)


def rsce_parse(blob: bytes) -> list[tuple[str, int, int]]:
    """[(name, first_block, size)] from the table."""
    if blob[:4] != RSCE_MAGIC:
        raise BootImageError(f"not an RSCE image (magic {blob[:4]!r})")
    tbl_offset, entry_blocks = blob[9], blob[10]
    count = struct.unpack_from("<I", blob, 12)[0]
    out = []
    for i in range(count):
        row = blob[(tbl_offset + i * entry_blocks) * RSCE_BLOCK:][:entry_blocks * RSCE_BLOCK]
        if row[:4] != RSCE_ENTRY:
            raise BootImageError(f"entry {i} has tag {row[:4]!r}, not {RSCE_ENTRY!r}")
        name = row[4:4 + RSCE_NAME_LEN].split(b"\0")[0].decode("ascii", "replace")
        first, size = struct.unpack_from("<II", row, 4 + RSCE_NAME_LEN)
        out.append((name, first, size))
    return out


def rsce_payload(blob: bytes, name: str) -> bytes:
    for entry_name, first, size in rsce_parse(blob):
        if entry_name == name:
            return blob[first * RSCE_BLOCK:first * RSCE_BLOCK + size]
    raise KeyError(name)


# ---------------------------------------------------------------------------------------------- cpio

def cpio_newc(members: list) -> bytes:
    """A `070701` newc archive (the kernel's initramfs format, usr/gen_init_cpio.c) from
    [(name, mode, payload)] -- payload bytes for a file, the target for a symlink, (major, minor)
    for a device node. Deterministic: inode numbers count up, mtime/uid/gid are 0, every name is
    relative."""
    out = bytearray()

    def member(ino, name, mode, data=b"", nlink=1, rdev=(0, 0)):
        raw = name.encode("utf-8") + b"\0"
        fields = (ino, mode, 0, 0, nlink, 0, len(data), 0, 0, rdev[0], rdev[1], len(raw), 0)
        out.extend(b"070701" + b"".join(b"%08x" % f for f in fields) + raw)
        out.extend(bytes(-len(out) % 4))
        out.extend(data)
        out.extend(bytes(-len(out) % 4))

    for ino, (name, mode, payload) in enumerate(members, start=1):
        kind = mode & 0o170000
        if kind == 0o040000:
            member(ino, name, mode, nlink=2)
        elif kind == 0o120000:
            member(ino, name, mode, payload.encode("utf-8"))
        elif kind in (0o020000, 0o060000):
            member(ino, name, mode, rdev=payload)
        elif kind == 0o100000:
            member(ino, name, mode, payload)
        else:
            raise ValueError("cpio member %r: unsupported mode %o" % (name, mode))
    member(0, "TRAILER!!!", 0)
    return bytes(out)


def boot_ramdisk() -> bytes:
    """A boot image's ramdisk with no /init: the kernel unpacks it, finds no init in it and mounts root= itself;
    /dev/console lets the first process open the console before devtmpfs is mounted. The loader wants a ramdisk."""
    members = [("dev", 0o040755, b""), ("dev/console", 0o020600, (5, 1)), ("root", 0o040700, b"")]
    blob = gzip.compress(cpio_newc(members), compresslevel=9, mtime=0)
    return blob + bytes(-len(blob) % 4)


# ---------------------------------------------------------------------------------------------- the image

def image_parts(blob: bytes) -> tuple[bytes, bytes, bytes]:
    """(kernel, ramdisk, second) of an Android v0 image."""
    ks, _, rs, _, ss = struct.unpack_from("<5I", blob, 8)
    r0 = PAGE + (ks + PAGE - 1) // PAGE * PAGE
    s0 = r0 + (rs + PAGE - 1) // PAGE * PAGE
    return blob[PAGE:PAGE + ks], blob[r0:r0 + rs], blob[s0:s0 + ss]


def check_kernel(image: bytes) -> dict:
    """An arm64 Image the loader can enter raw: its header magic, text_offset 0, and Image + BSS inside the loader's
    kernel reservation from KERNEL_ADDR."""
    if image[:2] == b"\x1f\x8b":
        raise BootImageError("kernel: gzip; this loader jumps into the payload raw (Image, not Image.gz)")
    if image[0x38:0x3C] != ARM64_MAGIC:
        raise BootImageError("kernel: no arm64 Image magic at +0x38")
    text_offset, image_size, flags = struct.unpack_from("<QQQ", image, 8)
    end = KERNEL_ADDR + max(image_size, len(image))
    if end > KERNEL_LIMIT:
        raise BootImageError(f"kernel: Image + BSS end at {end:#x}, past the loader's reservation {KERNEL_LIMIT:#x}")
    if text_offset != 0:
        raise BootImageError(f"kernel: text_offset {text_offset:#x}; a 6.12 Image has 0")
    return {"bytes": len(image), "image_size": hex(image_size), "flags": hex(flags), "end": hex(end),
            "md5": hashlib.md5(image).hexdigest()}


def dtb_facts(dtb: bytes) -> dict:
    """What a boot image needs of its DTB: the board (its root compatible, BOARDS; None when it names neither) and
    /chosen/bootargs."""
    try:
        tree = tu15_fdt.Tree(dtb)
    except ValueError as e:
        raise BootImageError(f"DTB: {e}") from None
    root = tree.nodes.get("/", {})
    found = {BOARDS[c] for c in root.get("compatible", b"").rstrip(b"\0").decode("ascii", "replace").split("\0")
             if c in BOARDS}
    if len(found) > 1:
        raise BootImageError(f"DTB: its compatible names {sorted(found)}: one board only")
    bootargs = tree.nodes.get("/chosen", {}).get("bootargs", b"").rstrip(b"\0").decode()
    model = tree.nodes.get("/", {}).get("model", b"").rstrip(b"\0").decode()
    return {"model": model, "board": found.pop() if found else None, "bootargs": bootargs}


def assemble(image: bytes, dtb: bytes, ramdisk: bytes, check_dtb=dtb_facts, cmdline: bytes | None = None
             ) -> tuple[bytes, dict]:
    """kernel + ramdisk + DTB (as the RSCE `second`) -> the `ANDROID!` v0 image with the loader's id. `check_dtb`
    reads the DTB (board, bootargs); a caller may hold it to more. `cmdline`: the header's command-line field
    (at most 511 bytes), which the loader never reads but its id covers; the DTB's bootargs when None."""
    kernel = check_kernel(image)
    if len(image) % 4:
        raise BootImageError("kernel: length not a whole number of words")
    dt = check_dtb(dtb)
    second = rsce_build([("rk-kernel.dtb", dtb)])
    if len(ramdisk) + len(second) > RAMDISK_SECOND_LIMIT:
        raise BootImageError(f"ramdisk + second = {len(ramdisk) + len(second)} bytes, over {RAMDISK_SECOND_LIMIT:#x}")
    # The loader builds the command line from the DTB, not from this field; it carries the DTB's line anyway,
    # unless the caller names what it holds (a chain image: its FIT, dho900_uboot_chain.py).
    if cmdline is None:
        cmdline = dt["bootargs"].encode()[:511]
    elif len(cmdline) > 511 or b"\0" in cmdline:
        raise BootImageError(f"header cmdline: {len(cmdline)} bytes or a NUL inside; the field holds 511 and its NUL")
    header = bytearray(struct.pack(
        "<8s10I16s512s32s1024s",
        b"ANDROID!", len(image), KERNEL_ADDR, len(ramdisk), RAMDISK_ADDR, len(second), 0,
        0, PAGE, 0, 0, BOARD_NAMES[dt["board"]], cmdline, bytes(32), bytes(1024)))
    header[BOOT_ID_OFFSET:BOOT_ID_OFFSET + 32] = boot_image_id(bytes(header), image, ramdisk, second)
    blob = pad(bytes(header)) + pad(image) + pad(ramdisk) + pad(second)
    if len(blob) > RECOVERY_BYTES:
        raise BootImageError(f"image is {len(blob)} bytes; `recovery` is {RECOVERY_BYTES}")
    return blob, {"kernel": kernel, "dtb": dt, "ramdisk_bytes": len(ramdisk), "second_bytes": len(second),
                  "bytes": len(blob)}


# ---------------------------------------------------------------------------------------------- the command line

def uboot_cmdline(bootargs: str, parm_cmdline: str, recovery_boot: bool = True) -> str:
    """The kernel command line the loader builds: the DTB's bootargs, the PARM's CMDLINE (on a `recovery` / `backup`
    boot with the parameter partition first in its partition list), then its own words."""
    cmd = parm_cmdline
    if recovery_boot:
        cmd = cmd.replace("mtdparts=rk29xxnand:", "mtdparts=rk29xxnand:" + RECOVERY_MTDPARTS_PREFIX, 1)
    return f"{bootargs} {cmd} {UBOOT_SUFFIX}"


def cmdline_budget(bootargs: str, parm_cmdline: str) -> dict:
    """The line a `recovery` / `backup` boot gives the kernel, held to the loader's 0x800-byte buffer (with its NUL)."""
    line = uboot_cmdline(bootargs, parm_cmdline, recovery_boot=True)
    if len(line.encode()) + 1 > UBOOT_CMDLINE_LIMIT:
        raise BootImageError(f"the loader's command line would be {len(line.encode()) + 1} bytes; it holds "
                             f"{UBOOT_CMDLINE_LIMIT:#x}")
    return {"kernel_cmdline": line, "bytes": len(line.encode()) + 1, "limit": UBOOT_CMDLINE_LIMIT}


# ---------------------------------------------------------------------------------------------- install

HEADER_CMDLINE = slice(64, 576)         # the header's cmdline[512], after magic, ten u32 and name[16]


def image_header(blob: bytes) -> dict:
    """The header fields an image carries, read back."""
    if blob[:8] != b"ANDROID!":
        raise BootImageError(f"not an Android boot image (magic {blob[:8]!r})")
    f = struct.unpack_from("<10I", blob, 8)
    if f[7] != PAGE:
        raise BootImageError(f"page size {f[7]}; this loader's images use {PAGE}")
    return {"kernel_size": f[0], "kernel_addr": f[1], "ramdisk_size": f[2], "ramdisk_addr": f[3],
            "second_size": f[4], "second_addr": f[5], "name": blob[48:64].split(b"\0")[0].decode("ascii", "replace"),
            "cmdline": blob[HEADER_CMDLINE].split(b"\0")[0]}


def image_dtb(blob: bytes) -> bytes:
    """The kernel's device tree: `rk-kernel.dtb` in the image's RSCE `second`."""
    try:
        return rsce_payload(image_parts(blob)[2], "rk-kernel.dtb")
    except KeyError:
        raise BootImageError("the image's resource section has no rk-kernel.dtb") from None


def id_holds(blob: bytes) -> bool:
    """Whether the image's id is the one the loader computes (it refuses an image whose id differs)."""
    kernel, ramdisk, second = image_parts(blob)
    return blob[BOOT_ID_OFFSET:BOOT_ID_OFFSET + 32] == boot_image_id(blob[:PAGE], kernel, ramdisk, second)


def show(blob: bytes) -> dict:
    h = image_header(blob)
    kernel, ramdisk, _ = image_parts(blob)
    dt = dtb_facts(image_dtb(blob))
    return {"name": h["name"], "kernel": check_kernel(kernel), "ramdisk_bytes": len(ramdisk),
            "board": dt["board"], "model": dt["model"], "bootargs": dt["bootargs"], "id_holds": id_holds(blob),
            "bytes": len(blob)}


def unpack(blob: bytes, out: Path) -> dict:
    image_header(blob)
    kernel, ramdisk, _ = image_parts(blob)
    out.mkdir(parents=True, exist_ok=True)
    parts = {"kernel": kernel, "ramdisk": ramdisk, "rk-kernel.dtb": image_dtb(blob),
             "cmdline.txt": image_header(blob)["cmdline"] + b"\n"}
    for name, data in parts.items():
        (out / name).write_bytes(data)
    return {name: len(data) for name, data in parts.items()}


def repack(blob: bytes, kernel: bytes | None = None, dtb: bytes | None = None, ramdisk: bytes | None = None
           ) -> tuple[bytes, dict]:
    """`blob` with the parts given replaced and its id recomputed; every other part, and the header's command-line
    field, as `blob` has them."""
    h = image_header(blob)
    if not id_holds(blob):
        raise BootImageError("the image's id does not hold: it is not an image this loader boots")
    old_kernel, old_ramdisk, _ = image_parts(blob)
    old_dtb = image_dtb(blob)
    if dtb is not None:
        want, got = dtb_facts(old_dtb), dtb_facts(dtb)
        if got["bootargs"] != want["bootargs"]:
            raise BootImageError("the new DTB's /chosen/bootargs differ from the image's; copy them over first:\n"
                                 "  fdtput -t s NEW.dtb /chosen bootargs \"$(fdtget OLD.dtb /chosen bootargs)\"")
        if got["board"] != want["board"]:
            raise BootImageError(f"the new DTB is for {got['board']}, the image for {want['board']}")
    return assemble(old_kernel if kernel is None else kernel, old_dtb if dtb is None else dtb,
                    old_ramdisk if ramdisk is None else ramdisk, cmdline=h["cmdline"])


def main(argv=None) -> int:
    import json
    ap = argparse.ArgumentParser(description="Read, unpack and repack the scope's boot images.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="an image's sections, board, command line and whether its id holds")
    s.add_argument("image", type=Path)
    u = sub.add_parser("unpack", help="kernel, ramdisk, rk-kernel.dtb and cmdline.txt into a directory")
    u.add_argument("image", type=Path)
    u.add_argument("dir", type=Path)
    r = sub.add_parser("repack", help="an image with the parts given replaced, its id recomputed")
    r.add_argument("image", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--kernel", type=Path, help="an arm64 Image (uncompressed)")
    r.add_argument("--dtb", type=Path, help="the board's DTB, carrying the image's /chosen/bootargs")
    r.add_argument("--ramdisk", type=Path, help="the initramfs as the image carries it (a gzip'd newc cpio)")
    a = ap.parse_args(argv)
    try:
        blob = a.image.read_bytes()
        if a.cmd == "show":
            print(json.dumps(show(blob), indent=1))
        elif a.cmd == "unpack":
            print(json.dumps(unpack(blob, a.dir), indent=1))
        else:
            read = (lambda p: p.read_bytes() if p else None)
            new, receipt = repack(blob, read(a.kernel), read(a.dtb), read(a.ramdisk))
            a.out.write_bytes(new)
            print(json.dumps({"out": str(a.out), "bytes": len(new), "kernel": receipt["kernel"],
                              "board": receipt["dtb"]["board"], "id_holds": id_holds(new),
                              "unchanged": new == blob}, indent=1))
    except (BootImageError, OSError) as e:
        print(f"rk_bootimg: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
