#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""Pack U-Boot's Maskrom RAM-boot images into a Rockchip loader file that `rkdeveloptool db` sends.

U-Boot's binman writes u-boot-rockchip-usb471.bin (the TPL: DRAM init, then back to the BootROM)
and u-boot-rockchip-usb472.bin (SPL with the whole FIT appended: BL31, U-Boot, its DT). In Maskrom
the RK3399 BootROM takes them as USB vendor requests 0x471 and 0x472.
`rkdeveloptool db FILE` sends the 471 and 472 entries of a loader file verbatim, 4 KiB per control
transfer with a CRC-CCITT trailer (rkdeveloptool RKDevice.cpp DownloadBoot, RKComm.cpp
RKU_DeviceRequest), so the file must hold them as Rockchip's packer stores them.

Rockchip's packers cannot take this 472: boot_merger (rockchip-linux/u-boot tools/rockchip) caps an
entry at 512 KiB and rkdeveloptool's own `pack` at 1 MiB, and the latter RC4s through a 16-bit
length (crc.cpp P_RC4), so an entry of 64 KiB or more comes out wrong. This packer writes the same
layout with neither limit:

  header   rk_boot_header, 102 bytes, packed (rkdeveloptool boot_merger.h / RKBoot.h): tag "BOOT",
           size 102, version BCD major.minor, merger version 0x01030000, release time (from
           SOURCE_DATE_EPOCH, UTC, so the file is reproducible), chip "330C" as boot_merger encodes
           RK3399 (convertChipType: four ASCII bytes, big-endian), one 471 entry, one 472 entry,
           NO loader entries, sign flag 0, rc4 flag 1 (what boot_merger writes without --rc4).
  entries  rk_boot_entry, 57 bytes each: size 57, type (1 = 471, 2 = 472), name UTF-16LE[20],
           data offset, data size (padded to 2048), delay in ms after sending it.
  data     each entry zero-padded to 2048 and RC4'd as one stream with Rockchip's fixed key
           (boot_merger.c P_RC4); the BootROM undoes it.
  crc      Rockchip CRC-32 (poly 0x04C10DB7, init 0, MSB first, no final xor) of all of the above,
           little-endian (crc.cpp CRC_32; RKBoot::CrcCheck).

No loader (FlashData / FlashBoot) entries: this file is for `db` only. A tool that writes a loader
to the card (`rkdeveloptool ul`, upgrade_tool UL) finds nothing to write in it and fails, instead of
writing an IDB nobody meant to write. The 471 delay (default 1000 ms) leaves the TPL time to train
the DRAM before the 472 request (an assumption: the BootROM cannot take 0x472 while the TPL runs).

    mkloader.py --471 u-boot-rockchip-usb471.bin --472 u-boot-rockchip-usb472.bin -o FILE
    mkloader.py --decode FILE --out-dir DIR     write DIR/471.bin, DIR/472.bin (RC4 undone)
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from pathlib import Path

TAG = 0x544F4F42                     # "BOOT"
MERGER_VERSION = 0x01030000
HEADER_SIZE = 102
ENTRY_SIZE = 57
ENTRY_ALIGN = 2048
ENTRY_471, ENTRY_472 = 1, 2
CHIP = "330C"                        # RK3399 in rkbin's RK3399MINIALL.ini [CHIP_NAME] NAME=RK330C
RC4_KEY = bytes([124, 78, 3, 4, 85, 5, 9, 7, 45, 44, 123, 56, 23, 13, 23, 17])


def rk_crc32(data: bytes) -> int:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C10DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    acc = 0
    for b in data:
        acc = ((acc << 8) & 0xFFFFFFFF) ^ table[((acc >> 24) ^ b) & 0xFF]
    return acc


def rc4(data: bytes) -> bytes:
    s = list(range(256))
    k = [RC4_KEY[i & 15] for i in range(256)]
    j = 0
    for i in range(256):
        j = (j + s[i] + k[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(data)
    i = j = 0
    for x in range(len(out)):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[x] ^= s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def bcd(v: int) -> int:
    return (((v // 10) % 10) << 4) | (v % 10)


def padded(blob: bytes) -> bytes:
    return blob + bytes(-len(blob) % ENTRY_ALIGN)


def entry(kind: int, name: str, offset: int, size: int, delay_ms: int) -> bytes:
    raw = name.encode("utf-16-le")[: 2 * 20].ljust(40, b"\0")
    return struct.pack("<BI40sIII", ENTRY_SIZE, kind, raw, offset, size, delay_ms)


def build(tpl: bytes, spl_fit: bytes, delay_471: int, epoch: int, major: int = 1, minor: int = 0) -> bytes:
    t = time.gmtime(epoch)
    data = [padded(tpl), padded(spl_fit)]
    off = HEADER_SIZE + 2 * ENTRY_SIZE
    head = struct.pack("<IHII", TAG, HEADER_SIZE, (bcd(major) << 8) | bcd(minor), MERGER_VERSION)
    head += struct.pack("<HBBBBB", t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    chip = int.from_bytes(CHIP.encode("ascii"), "big")
    head += struct.pack("<I", chip)
    head += struct.pack("<BIB", 1, HEADER_SIZE, ENTRY_SIZE)
    head += struct.pack("<BIB", 1, HEADER_SIZE + ENTRY_SIZE, ENTRY_SIZE)
    head += struct.pack("<BIB", 0, HEADER_SIZE + 2 * ENTRY_SIZE, ENTRY_SIZE)
    head += struct.pack("<BB", 0, 1) + bytes(57)
    assert len(head) == HEADER_SIZE
    body = head
    body += entry(ENTRY_471, "u-boot-tpl", off, len(data[0]), delay_471)
    body += entry(ENTRY_472, "u-boot-spl-fit", off + len(data[0]), len(data[1]), 0)
    body += rc4(data[0]) + rc4(data[1])
    return body + struct.pack("<I", rk_crc32(body))


def decode(blob: bytes) -> dict:
    if struct.unpack_from("<I", blob, 0)[0] != TAG:
        raise SystemExit("not a BOOT loader file")
    if struct.unpack_from("<I", blob, len(blob) - 4)[0] != rk_crc32(blob[:-4]):
        raise SystemExit("loader CRC mismatch")
    out = {}
    for kind, at in ((471, 0x19), (472, 0x1F)):
        count, eoff, esize = struct.unpack_from("<BIB", blob, at)
        for i in range(count):
            size_, typ, name, doff, dsize, delay = struct.unpack_from("<BI40sIII", blob, eoff + i * esize)
            out[f"{kind}_{i}"] = {"name": name.decode("utf-16-le").rstrip("\0"), "delay_ms": delay,
                                  "data": rc4(blob[doff:doff + dsize])}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--471", dest="tpl", type=Path)
    ap.add_argument("--472", dest="spl", type=Path)
    ap.add_argument("--delay-471", type=int, default=1000, help="ms to wait after the 471 request (default 1000)")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--decode", type=Path)
    ap.add_argument("--out-dir", type=Path)
    args = ap.parse_args()
    if args.decode:
        entries = decode(args.decode.read_bytes())
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for key, e in entries.items():
            (args.out_dir / f"{key.split('_')[0]}.bin").write_bytes(e["data"])
            print(f"{key}: {e['name']} {len(e['data'])} B, delay {e['delay_ms']} ms")
        return 0
    if not (args.tpl and args.spl and args.output):
        ap.error("--471, --472 and -o are required")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    blob = build(args.tpl.read_bytes(), args.spl.read_bytes(), args.delay_471, epoch)
    args.output.write_bytes(blob)
    print(f"{args.output}: {len(blob)} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
