# Open first stage for the RK3399 scopes (development preview)

**This is a development preview. It is not part of any NovaOS release, no release installs it, and nothing here
writes a scope's storage.**

This directory is the source of a boot chain for the Rigol DHO800/DHO900 and MHO900 oscilloscopes (Rockchip RK3399)
that is open from the first instruction after the BootROM:

- **U-Boot TPL** with U-Boot's own open LPDDR4 init (`drivers/ram/rockchip/sdram_rk3399.c`): no Rockchip DDR blob;
- **U-Boot SPL**;
- **Trusted Firmware-A BL31** v2.15.0, unchanged, with its Cortex-M0 power-management firmware built from source;
- **U-Boot proper**, the same board code as the U-Boot NovaOS boots through today (release r115 of this repository),
  built here as its own first stage instead of being started by the scope's original loaders.

All of it is mainline U-Boot v2026.10 with the 17 patches in `patches/`, and TF-A as Arm publishes it.

## Status (2026-10-29)

Proven on a DHO924S, each time booted from RAM over USB in the RK3399's Maskrom mode, with nothing written to the
scope's storage:

- **The TPL's LPDDR4 init trains the scope's memory**: two CXMT CXDB4CBAM parts, LPDDR4, 2 channels x 32 bit,
  1 rank, 4 GiB. It trains at 400 MHz and runs at 800 MHz by default, and at 856 MHz with patch 0012
  (`NOVA_RK3399_DDR_MHZ=856`).
- **The whole open chain boots NovaOS**: TPL, SPL, BL31 and U-Boot proper came up from the USB download, and U-Boot
  proper booted NovaOS from the scope's card. Linux reported 3,922,200 kB of memory. The default build (no test
  option) did this at 800 MHz.
- **memtester 3000M passes at 800 MHz and at 856 MHz**: memtester 4.6.0, 3,000 MiB, one loop, every test passed.
  Both runs used the trace option (`NOVA_RK3399_TPL_TRACE=1`, patch 0015), which runs the same LPDDR4 init under
  the watchdog and leaves breadcrumbs NovaOS can read after the boot.
- **The earlier "hang" in TPL was the BootROM download flag.** Resetting into Maskrom from the loader's USB mode
  leaves the download request in a register, and U-Boot's stages answer it by going straight back to the BootROM,
  so the host's download failed. Patch 0014 lets a stage that was itself loaded over USB carry on; a boot from the
  card handles the flag as before.

Measured in one of those boots (from the kernel's `/bootstage`, in time from TPL's start): the LPDDR4 init took
101 ms (with the trace option), SPL started at 19.8 s (the USB download of SPL and the FIT through the BootROM's
control transfers takes about 19.7 s), U-Boot proper at 20.0 s, and the kernel at 21.2 s.

Not done yet:

- **The MHO900 builds have not run on an MHO900**, not even from RAM. They pass the build's gates only.
- **Writing this first stage to storage is not supported, and has not been tried.** Do not write `idbloader.img`,
  `u-boot.itb` or `u-boot-rockchip.bin` to a scope's card. A first stage on the card replaces the scope's own boot
  path, and a wrong one leaves the scope unable to boot until it is recovered over USB in Maskrom mode.
- Starting the scope's original firmware through this chain (under upstream TF-A instead of the BL31 it was built
  with) has not been tried.
- The timing options of patches 0016 and 0017 are built and gated but have not run on a scope.

## What it builds

`bash build.sh --out out` writes, for each family (`out/dho900/`, `out/mho900/`):

| File | What |
|---|---|
| `rigol-<family>-maskrom-loader.bin` | the RAM boot: a Rockchip loader file for `rkdeveloptool db`, holding the TPL (USB request 0x471) and SPL with the FIT (0x472), and no loader entries, so a tool that writes a loader to the card has nothing to write |
| `u-boot-rockchip-usb471.bin`, `u-boot-rockchip-usb472.bin` | the same two stages as raw images |
| `idbloader.img`, `u-boot.itb`, `u-boot-rockchip.bin` | the card images (TPL + SPL for LBA 64; U-Boot proper, BL31 and the control device tree for LBA 2048; both in one). Built and checked, **not supported on a card yet** |
| `u-boot-tpl.bin`, `u-boot-spl.bin`, `u-boot.bin`, `u-boot.dtb`, the ELF, maps, `final.config`, `u-boot-initial-env` | the parts, for reading |
| `uboot-gates.json`, `ldr_check.txt` | U-Boot proper's gates; rkdeveloptool's own parser reading the loader file |

and `out/tf-a/` (BL31 and the M0 firmware), `out/build-manifest.json` (every input, digest and gate result) and
`out/SHA256SUMS`.

Every gate fails the build. `gates.py` checks that the card images fit their places (LBA 64-535 and 2048-7167),
that the board code writes nothing to the card and reads the reboot flag the way the scope's systems set it, that
nothing loads over the memory NovaOS keeps its crash records in, and that the loader file decodes to exactly the two
images; `check-uboot-rk3399.py` holds U-Boot proper to its configuration, symbol and device-tree rules.

## Building

On Linux x86_64. On Debian or Ubuntu the host packages are:

    sudo apt install build-essential bc bison flex gawk git curl xz-utils libssl-dev uuid-dev \
        libgnutls28-dev python3-dev python3-setuptools

Then:

    bash build.sh --out out

`build.sh` fetches every input by its pin in `pins.env` and refuses anything else: U-Boot v2026.10 (commit
`5508406582f6`) and TF-A v2.15.0 (`da738d5eae93`) by commit, rkdeveloptool (`304f0737`, for its loader parser),
Arm GNU Toolchain 14.2.Rel1 (aarch64-none-elf and arm-none-eabi) and the pyelftools and SWIG wheels by sha256.
They go into `~/nova-rk3399-spl` (or `$NOVA_RK3399_SPL_WS`), so only the first build downloads them. It then
applies `patches/0001-0017` and builds in `/tmp/nova-rk3399-spl`, one fixed path with `SOURCE_DATE_EPOCH` set to the
U-Boot commit's date, so every host makes the same images.

The default build ends with `build.sh: 23 of 23 binaries are the expected bytes (expected.sha256)`. Its DHO800/DHO900
TPL and SPL are byte for byte the ones the DHO924S ran in the RAM boots above, and its U-Boot proper differs from the
one that ran only in the 12-character build id it logs at entry (the sha256 of this directory's scripts and patches).

Test options, each off by default, by environment:

| Variable | Effect |
|---|---|
| `NOVA_RK3399_DDR_MHZ=856` | the LPDDR4 at 856 MHz after the 400 MHz training (patch 0012) |
| `NOVA_RK3399_DDR_MHZ=400` | TPL stops after the 400 MHz training (patch 0013) |
| `NOVA_RK3399_TPL_TRACE=1` | TPL's LPDDR4 init under the watchdog, with breadcrumbs in PMU_SYS_REG0-3 and PMUGRF OS_REG3 (patch 0015) |
| `NOVA_RK3399_TPL_OPTS="trfc280 padspeed grfie"` | the timing options of patches 0016 and 0017, any of them |

## How the RAM boot test is run

A RAM boot writes nothing: the BootROM loads the stages into the SoC's memory, and U-Boot proper then boots
whatever the card holds, as usual. If it does not come up, power-cycle the scope and it boots its own first stage from
the untouched card.

1. Build (above), and take `out/dho900/rigol-dho900-maskrom-loader.bin`.
2. Connect the scope's rear USB device port (USB-B) to a Linux PC with `rkdeveloptool`.
3. Put the scope's loader in its USB mode. From NovaOS: `systemctl reboot --reboot-argument=loader`.
4. Reset into the BootROM's Maskrom: `rkdeveloptool rd 3`. Then `rkdeveloptool ld` must list exactly one device, in
   Maskrom mode.
5. Send the loader file: `rkdeveloptool db rigol-dho900-maskrom-loader.bin`. TPL trains the memory and returns to the
   BootROM, which then takes SPL and the FIT (about 20 seconds); SPL starts BL31 and U-Boot proper from that download.
   NovaOS came up about 40 seconds after the download began.
6. On NovaOS: `grep MemTotal /proc/meminfo` and `busybox devmem 0xff320308 32` (PMUGRF OS_REG2, the memory layout TPL
   recorded: `0x32C1F2C1` is LPDDR4, 2 channels, 1 rank, 4 GiB), then a memory test such as `memtester 3000M 1`.

One download per Maskrom entry: a download that got as far as the memory init leaves the controller running, and a
second one into it hangs. Power-cycle before trying again.

The bench runs used Rockchip's `upgrade_tool` for steps 4 and 5 (`RD 3`, `DB`). `rkdeveloptool rd 3` and
`rkdeveloptool db` are the open equivalents, and the build opens the loader file with rkdeveloptool's own parser and
checks the bytes it would send, but rkdeveloptool is not the tool the bench used.

With only the scope's original firmware on the card, U-Boot proper starts that firmware, which has not been tried
under upstream TF-A (see "Not done yet").

## Files

| File | What |
|---|---|
| `patches/0001-0010` | U-Boot proper for the board `rigol/nova_rk3399`: 0001-0007 as in release r115 (0001 now also keeps the arm64 Image header out of TPL and SPL), 0008 a backport of upstream U-Boot's commit `a1f39f70` (Alexey Charkov), 0009 an option for a board without a download key, 0010 this first stage: TPL, SPL and BL31 before U-Boot proper (`nova-mainline.config`), and what the scope's systems expect of their loader (the reboot flag, Run/Stop at power-on, the partition order, Loader mode) |
| `patches/0011` | SPL: dw_mmc polls the FIFO every microsecond, not every 200 |
| `patches/0012-0015` | the LPDDR4 at 856 MHz (0012) and at its 400 MHz training rate (0013), test options; a stage loaded over USB does not go back for the download flag (0014, always on); the TPL trace (0015, a test option) |
| `patches/0016-0017` | timing options: tRFCab 280 ns for 8 Gb per channel dies (0016); the PHY pad speed and the GRF polarity write where U-Boot's port differs from Rockchip's own DDR code (0017) |
| `build.sh`, `pins.env` | the build and its pinned inputs |
| `gates.py`, `check-uboot-rk3399.py` | the gates |
| `tools/mkloader.py` | writes the Maskrom loader file (Rockchip's loader layout, without the size limits of Rockchip's own packers) |
| `tools/ldr_check.cpp` | opens that file with rkdeveloptool's own parser, as `db` does |
| `tools/fdt_tree.py` | reads a device tree for the gates |
| `expected.sha256` | the default build's binaries |

## License and credit

U-Boot is licensed under GPL-2.0-or-later. The patches are under the license of the files they change; the files they
create are `GPL-2.0+`, and the board's device trees `GPL-2.0+ OR MIT`. Patch 0008 is Alexey Charkov's upstream U-Boot
change, backported unchanged. The scripts and tools here are `GPL-2.0-or-later`; `tools/mkloader.py` follows the
loader-file format of rkdeveloptool (GPL-2.0), and `tools/ldr_check.cpp` is built against rkdeveloptool's own
`RKImage.cpp`, `RKBoot.cpp` and `crc.cpp`, fetched at build time. Trusted Firmware-A is licensed under BSD-3-Clause;
its BL31 is part of `u-boot.itb`, and anyone who passes on a `u-boot.itb` must keep TF-A's copyright notice and
license with it (`docs/license.rst` in the TF-A tree). Nothing of U-Boot, TF-A or rkdeveloptool is copied into this
directory: they are fetched by commit.

Copyright (C) 2026 Olympian LED, Inc. for the patches, the files they create, and the scripts and tools here.
Author: Brady <brady@aioscilloscope.com>. Keep these notices when you redistribute them.
