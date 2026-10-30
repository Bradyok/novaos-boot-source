# NovaOS boot source

The source of the boot loaders NovaOS builds for the Rigol DHO800/DHO900 and MHO900 oscilloscopes (Rockchip RK3399):
mainline U-Boot with our patches, and Trusted Firmware-A. U-Boot is published under the GNU General Public License,
version 2 or later; Trusted Firmware-A under the BSD 3-Clause license.

Each release is a tag here with a GitHub Release attached, its complete source as the archive:

| Release | U-Boot | Trusted Firmware-A | Boards | Complete source |
|---|---|---|---|---|
| [r115](https://github.com/Bradyok/novaos-boot-source/releases/tag/r115) | v2026.10, 10 patches (3 first-stage, 7 uboot-proper) | v2.15.0, unchanged | DHO800 / DHO900, MHO900 | `u-boot-rk3399-novaos-r115.tar.xz` |
| [r131](https://github.com/Bradyok/novaos-boot-source/releases/tag/r131) | v2026.10, 11 patches (11 uboot-proper) | not part of it | DHO800 / DHO900, MHO900 | `u-boot-rk3399-novaos-r131.tar.xz` |

The kernel these boot loaders start is published in
[novaos-kernel-source](https://github.com/Bradyok/novaos-kernel-source).

## What is current

- **U-Boot proper, `chainload`** is the boot loader NovaOS releases ship. NovaOS installs it in the scope's `recovery`
  partition, where the scope's own boot loader starts it; the scope's own boot loaders and firmware stay in place.
  The latest here is r131's, build `0a66aa1d326f`, U-Boot v2026.10 with 11 patches ([`r131/boot/uboot-proper/`](r131/boot/uboot-proper/)).
- **The card's first stage**, [`development/rk3399-first-stage/`](development/rk3399-first-stage/): build `7e3450e11aa6`: U-Boot TPL with U-Boot's own LPDDR4 init (no Rockchip DDR blob), which runs the memory at 856 MHz with the boards' own LPDDR4 timing set, SPL, TF-A v2.15.0 BL31 and U-Boot proper, all built from source (U-Boot v2026.10 with 19 patches, the first 11 the same as U-Boot proper's). Its archive is attached to the GitHub Release [`first-stage-7e3450e11aa6`](https://github.com/Bradyok/novaos-boot-source/releases/tag/first-stage-7e3450e11aa6).
  `idbloader.img` (U-Boot TPL and SPL) goes to card LBA 64 and `u-boot.itb` (TF-A BL31, U-Boot proper and its device
  tree) to LBA 2048. It is in no release: no NovaOS release writes it to a card. A first stage written to a card
  replaces the scope's own boot path, and a wrong one can leave the scope unable to boot until it is recovered over
  USB in Maskrom mode: save the card's head first (below).
- **Retired:** the first stage of [`r115/boot/first-stage/`](r115/boot/first-stage/) was built and checked by its build's own gates and never ran on a scope. Do not write it to a card.

## On the card

idbloader.img goes to LBA 64 and u-boot.itb to LBA 2048; u-boot-rockchip.bin is both, for one write at LBA 64. Only LBA 64-7167 is ever written, u-boot.itb's region (2048-7167) first and the idbloader's (64-2047) last: the IDB header at LBA 64 is the commit point, and until it is written the card still boots its own chain. Nothing at LBA 7168 or beyond may be touched: 7168-7679 is the vendor storage (the unit's MAC address), 8064-16383 the scope's U-Boot environment and its eight copies of the partition table (PARM), and from LBA 16384 on the scope's own partitions (uboot, trust, misc, ..., the calibration, userdata). Never write at LBA 16384, U-Boot's default raw U-Boot sector (0x4000): that is the scope's uboot partition. Save LBA 0-32767 before the first write; going back is the saved LBA 64-7167, written the same way.

The partition table. U-Boot proper takes every LBA it reads from its build, so it first checks the card's PARM copy at LBA 8192: the first 0x3ae (942) bytes there must have the crc32 0x4a377dc5, which both scope families' cards have. A card whose table differs (a resized userdata) boots into Loader mode (Rockusb) every time. To check a card from Linux (BusyBox too: gzip's trailer is that crc32), this prints 4a377dc5:

    dd if=/dev/mmcblk0 bs=512 skip=8192 count=2 2>/dev/null | head -c 942 | gzip -1 | tail -c 8 | head -c 4 | od -An -tx4

The boot record. Each boot of the committed image in `recovery` writes a record to PMUGRF OS_REG1 (0xff320304) before it starts, and NovaOS clears it once the boot has stayed up. A system of your own booted through this U-Boot must clear it too, once it is up, or after three plain reboots in a row the fourth boot is the last rung, the scope's original firmware (a power-on clears it as well):

    busybox devmem 0xff320304 32 0

Getting to Maskrom from Linux, with this first stage on the card: the download flag 0xEF08A53C in PMUGRF OS_REG0 (0xff320300), then a reset by the watchdog WDT0 (0xff848000; it must be running, its control register there reading 1, as NovaOS's kernel leaves it): timeout 0, then a restart. That reset keeps OS_REG0, and the TPL sends the BootROM to its download mode (USB 2207:330c):

    busybox devmem 0xff320300 32 0xEF08A53C; busybox devmem 0xff848004 32 0; busybox devmem 0xff84800c 32 0x76

A plain reboot loses the flag: a kernel with Rockchip's reboot-mode pre-restart hook (NovaOS's has it) writes the normal-boot flag over it at every restart, sysrq 'b' included. Check every digit: with the scope's own U-Boot on the card, 0x5242C306 and 0x5242C307 in OS_REG0 ask it to wipe data.

Rockchip's prebuilt BL31 (rkbin rk3399_bl31_v1.36.elf) in place of TF-A's needs CONFIG_SPL_ATF_NO_PLATFORM_PARAM=y: it reads the second argument SPL passes as Rockchip's parameter list and hangs on the device tree SPL puts there. The packages here build TF-A's BL31 and leave that option off.

## Building

Each archive unpacks to `README.txt` (the written offer), `boot/` and `SHA256SUMS` (every file). `boot/src/` holds the
complete source tree of each component with our patches already applied, and `src/UPSTREAM.sha256` every file of
them that is upstream's unchanged. Each lane's `patches/` has our changes as patches, to read. `boot/build.sh`
rebuilds every binary with no network and compares each with `boot/expected.sha256` byte for byte. `boot/BUILDING.txt`
gives every step and the exact compilers, and `boot/FACTS.env` names the upstream commits, the source date and the
compilers. On Linux x86_64:

    tar -xJf u-boot-rk3399-novaos-<release>.tar.xz && cd u-boot-rk3399-novaos-<release>
    # BUILDING.txt: the compilers (and Arm's two toolchains for the first stage), the host packages
    bash boot/build.sh --check

It ends with `build.sh: N of N binaries are the shipped bytes`. A release's recovery images are rebuilt only from
that release's own stage-B images (`--stage-b dho900=FILE --stage-b mho900=FILE`), which are not part of the source.
The same packages are readable here, each in its directory: all of them but the source trees and the `SHA256SUMS`
that lists them.

## License and credit

U-Boot is licensed under GPL-2.0-or-later and Trusted Firmware-A under BSD-3-Clause; each file names its own license,
and the trees carry the license texts (`src/u-boot-*/Licenses/`, `src/tf-a-*/docs/license.rst`). Our patches are under
the license of the files they change; the files they create are `GPL-2.0+`, the board device trees `GPL-2.0+ OR MIT`,
and the scripts in `install/` `GPL-2.0-or-later`.

Copyright (C) 2026 Olympian LED, Inc. for the patches, the files they create and the scripts in `install/`. Author:
Brady <brady@aioscilloscope.com>. Keep these notices when you redistribute them.

## Written offer

For three years from the date you received a NovaOS release, Olympian LED, Inc. will give anyone a copy of that release's
boot loader source on a physical medium for no more than the cost of making and sending it. Write to Olympian LED, Inc.,
brady@aioscilloscope.com, naming the release.
