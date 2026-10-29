# NovaOS boot source

The source of the boot loaders NovaOS builds for the Rigol DHO800/DHO900 and MHO900 oscilloscopes (Rockchip RK3399):
mainline U-Boot with our patches, and Trusted Firmware-A. U-Boot is published under the GNU General Public License,
version 2 or later; Trusted Firmware-A under the BSD 3-Clause license.

Each release is a tag here with a GitHub Release attached:

| Release | U-Boot | Trusted Firmware-A | Boards | Complete source |
|---|---|---|---|---|
| [r115](https://github.com/Bradyok/novaos-boot-source/releases/tag/r115) | v2026.10, 10 patches (3 first stage, 7 U-Boot proper) | v2.15.0, unchanged | DHO800 / DHO900, MHO900 | `u-boot-rk3399-novaos-r115.tar.xz` |

The kernel these boot loaders start is published in
[Bradyok/novaos-kernel-source](https://github.com/Bradyok/novaos-kernel-source).

## Development preview: the open first stage

[`development/rk3399-first-stage/`](development/rk3399-first-stage/) is not part of any release. It is the source
of a first stage that is open from the BootROM on: U-Boot TPL with U-Boot's own LPDDR4 init (no Rockchip DDR blob),
SPL, Trusted Firmware-A BL31 and U-Boot proper, with its own `build.sh` and README. On 2026-10-29 it booted NovaOS on a
DHO924S from RAM over USB, with the memory at 800 MHz and at 856 MHz and memtester passing at both. Nothing was written
to the scope's storage, and writing this first stage there is not supported yet.

## The download

`u-boot-rk3399-novaos-r115.tar.xz` (70 MB) is the complete source, with nothing to fetch but the compilers:

- `boot/src/`: the complete source tree of each component with our patches already applied:
  `u-boot-first-stage` and `u-boot-uboot-proper` (U-Boot v2026.10 with each lane's patches),
  `tf-a-v2.15.0` (Trusted Firmware-A as built) and `lz4-1.10.0` (the compressor the recovery image's kernel is packed
  with). `src/UPSTREAM.sha256` lists every file that is upstream's unchanged;
- `boot/first-stage/patches/` and `boot/uboot-proper/patches/`: our changes as patches, to read;
- `boot/install/`: the scripts that pack the recovery image;
- `boot/build.sh`: rebuilds every binary with no network and compares each with the released one byte for byte;
  `boot/BUILDING.txt` gives every step and the exact compilers; `boot/FACTS.env` names the upstream commits, the
  source date and the compilers; `boot/expected.sha256` the binaries;
- `README.txt`: the written offer; `SHA256SUMS`: every file.

The same release is readable here under `<release>/`: all of it but the source trees (`boot/src/`) and the
`SHA256SUMS` that lists them.

## What it builds

| Lane | Binaries (each for the DHO800/DHO900 and the MHO900) |
|---|---|
| first-stage | `idbloader.img` (U-Boot TPL and SPL) for card sector 64, `u-boot.itb` (U-Boot, TF-A BL31 and the device tree) for sector 2048, `u-boot-rockchip.bin` (both, for one write at sector 64), `u-boot-rockchip-usb471.bin` / `-usb472.bin` (a boot from RAM over USB in Maskrom mode), and TF-A's `bl31.elf` with its Cortex-M0 power-management firmware |
| uboot-proper | `u-boot.bin` on its own, in two builds: `chainload`, which the scope's own boot loader starts from the `recovery` partition, and `kexec-test`, which a running NovaOS starts with kexec; and `recovery-uboot-chain.img`, the `recovery` image that carries the chainload `u-boot.bin` and NovaOS r115's kernel, device tree and initramfs |

## Status

- **U-Boot proper (`chainload`)** is the boot loader NovaOS boots through: NovaOS installs it in the `recovery`
  partition, tries each new one from a spare slot under the watchdog (three attempts, then the previous one twice,
  then the scope's original firmware), and keeps the scope's own boot loaders and firmware in place. It starts the
  kernel 2.3 s sooner than the scope's own U-Boot (on the arch timer from reset, median of six boots: DHO924S 4.79 to
  2.52 s, MHO934 4.90 to 2.62 s; power-on to the scope screen in r115 10.68 s and 12.19 s), and hands the scope's boot
  logo to the kernel as the original boot does. In r115 it is qualified on the DHO924S and MHO934 bench
  units: six-reboot boot time, health and idle eMMC writes pass on both.
- **r115's first stage** (TPL, SPL, TF-A) is built and checked by its build's own gates only. It has not run on a
  scope. (The development preview above is a newer first stage, and it has run from RAM.)
- A boot loader written to a scope's card replaces the scope's own boot path, and a wrong one can leave the scope
  unable to boot until it is recovered over USB in Maskrom mode. These files are published as source.

## Building

On Linux x86_64 (BUILDING.txt has every command):

    tar -xJf u-boot-rk3399-novaos-r115.tar.xz && cd u-boot-rk3399-novaos-r115
    # BUILDING.txt step 1: Arm's two toolchains into toolchains/, each checked by its sha256
    # BUILDING.txt step 2: the host packages (on Debian or Ubuntu, one apt line)
    bash boot/build.sh --toolchains "$PWD/toolchains" --check

It ends with `build.sh: N of N binaries are the shipped bytes`. With the compilers BUILDING.txt names, every binary
is the released one byte for byte, whatever directory it is built in. The two recovery images are rebuilt only from
NovaOS r115's own stage-B image (`--stage-b dho900=FILE --stage-b mho900=FILE`), which is not part of this source.

## License and credit

U-Boot is licensed under GPL-2.0-or-later and Trusted Firmware-A under BSD-3-Clause; each file names its own
license, and the trees carry the license texts (`src/u-boot-*/Licenses/`, `src/tf-a-v2.15.0/docs/license.rst`). Our
patches are under the license of the files they change; the files they create are `GPL-2.0+`, the board device trees
`GPL-2.0+ OR MIT`, and the scripts in `install/` `GPL-2.0-or-later`.

Copyright (C) 2026 Olympian LED, Inc. for the patches, the files they create and the scripts in `install/`. Author:
Brady <brady@aioscilloscope.com>. Keep these notices when you redistribute them.

## Written offer

For three years from the date you received a NovaOS release, Olympian LED, Inc. will give anyone a copy of that
release's boot loader source on a physical medium for no more than the cost of making and sending it. Write to
Olympian LED, Inc., brady@aioscilloscope.com, naming the release.
