NovaOS's development first stage, build first-stage-7e3450e11aa6 (in no release) -- the RK3399 boot chain: GPL source
==================================================

Copyright (C) 2026 Olympian LED, Inc.

U-Boot is free software under the GNU General Public License version 2 or later; Trusted
Firmware-A, whose BL31 is part of u-boot.itb, is under the BSD 3-Clause license. Each file names
its own license; our changes are under the license of the file they change.

This directory is the complete corresponding source of the boot loaders built for NovaOS's development first stage, build first-stage-7e3450e11aa6 (in no release)
on the Rigol DHO800/DHO900 and MHO900:

  boot/     first-stage: idbloader.img and u-boot.itb (U-Boot TPL, SPL and U-Boot proper, TF-A BL31);
            src/ holds the complete source tree of each component with our patches applied
            (u-boot-first-stage: U-Boot v2026.10 with our patches, tf-a-v2.15.0: Trusted Firmware-A
            v2.15.0); each lane's patches/ lists our changes; build.sh rebuilds every shipped boot
            binary from exactly these with no network, BUILDING.txt names the compilers it needs,
            expected.sha256 the shipped binaries it reproduces byte for byte.

WRITTEN OFFER. For three years from the date you received this release, Olympian LED, Inc. will give anyone
a copy of this source on a physical medium for no more than the cost of making and sending it.
Write to Olympian LED, Inc., brady@aioscilloscope.com, naming the release (first-stage-7e3450e11aa6).
