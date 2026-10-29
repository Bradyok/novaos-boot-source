#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
#
# Build the open first stage for the Rigol DHO800/DHO900 and MHO900 (RK3399), one set per family:
#
#   TPL      U-Boot v2026.10, the open LPDDR4 init (no rkbin blob)          \ idbloader.img (card LBA 64)
#   SPL      U-Boot v2026.10                                                /
#   BL31     TF-A v2.15.0 PLAT=rk3399, its Cortex-M0 PMU firmware built     \ u-boot.itb (card LBA 2048)
#   U-Boot   U-Boot proper, the board rigol/nova_rk3399 (nova-mainline.config) /
#   Maskrom  u-boot-rockchip-usb471.bin / usb472.bin and a loader file `rkdeveloptool db` sends (a boot from RAM)
#
#   bash build.sh --out DIR [--families "dho900 mho900"]
#
# BUILD ONLY: nothing here talks to a scope or writes a card. Linux x86_64. Every input is pinned in pins.env and
# refused unless it is those bytes: U-Boot, TF-A and rkdeveloptool are fetched by commit, Arm's toolchains and the
# two Python wheels by sha256, into $NOVA_RK3399_SPL_WS (default ~/nova-rk3399-spl), which later builds reuse.
# The tree is built under one fixed path (/tmp/nova-rk3399-spl, one build per host at a time) with
# SOURCE_DATE_EPOCH = the U-Boot commit's date, so two hosts produce the same images.
#
# Test options, all off by default, by environment (each build logs its variant, and refuses one whose option
# does not reach the final .config):
#   NOVA_RK3399_DDR_MHZ=800|856|400   the LPDDR4's final rate (856: patches/0012; 400: stop after the 400 MHz
#                                     training, patches/0013)
#   NOVA_RK3399_TPL_TRACE=1           TPL's LPDDR4 init under the watchdog, with breadcrumbs (patches/0015)
#   NOVA_RK3399_TPL_OPTS="trfc280 padspeed grfie"   the timing options of patches/0016 and 0017, any of them
#
# Needs on the host: git make gcc g++ bison flex python3 (setuptools, its C headers) curl tar xz sha256sum
# timeout flock, and the openssl, gnutls and uuid headers for U-Boot's host tools (the README's "Building").
set -euo pipefail

die() { echo "FATAL: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/pins.env"

OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --families) FAMILIES="$2"; shift 2 ;;
        *) die "unknown argument $1" ;;
    esac
done
[ -n "$OUT" ] || die "--out DIR is required"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WS="${NOVA_RK3399_SPL_WS:-$HOME/nova-rk3399-spl}"
B=/tmp/nova-rk3399-spl
JOBS="${JOBS:-$(( $(nproc) < 8 ? $(nproc) : 8 ))}"
LOG="$OUT/build.log"
: > "$LOG"
say() { echo "== $*" | tee -a "$LOG"; }

for tool in git make gcc g++ bison flex python3 curl tar xz sha256sum timeout flock; do
    command -v "$tool" >/dev/null || die "required tool unavailable: $tool"
done
python3 -c 'import setuptools' 2>/dev/null || die "python3 setuptools missing (U-Boot builds pylibfdt with it)"
python3 -c 'import sysconfig, os, sys; sys.exit(not os.path.exists(os.path.join(sysconfig.get_paths()["include"], "Python.h")))' \
    || die "python3 C headers missing (pylibfdt)"

exec 9>/tmp/nova-rk3399-spl.lock
flock -n 9 || die "another build holds /tmp/nova-rk3399-spl.lock on this host"
rm -rf "$B"
mkdir -p "$B" "$WS"

# The build's identity, logged by U-Boot proper at entry (CONFIG_NOVA_BUILD_ID): the sha256 of what it is built
# from, this directory's scripts, tools and patches.
BUILD_ID="$( (cd "$HERE" && cat pins.env build.sh gates.py check-uboot-rk3399.py tools/*.py tools/*.cpp \
              patches/*.patch) | sha256sum | cut -c1-12)"
echo "build id $BUILD_ID" | tee -a "$LOG"

# --- pinned compilers ---------------------------------------------------------------------------
toolchain() {   # name sha256 -> prints the bin dir
    local tb="$WS/tc/$1.tar.xz" dir="$WS/tc/$1"
    mkdir -p "$WS/tc"
    if [ ! -x "$dir/bin" ] || [ ! -f "$dir/.verified-$2" ]; then
        [ -f "$tb" ] && [ "$(sha256sum "$tb" | cut -d' ' -f1)" = "$2" ] \
            || curl -fsSL -o "$tb" "$ARM_TC_BASE/$1.tar.xz" || die "fetch $1"
        [ "$(sha256sum "$tb" | cut -d' ' -f1)" = "$2" ] || die "$1.tar.xz is not sha256 $2"
        rm -rf "$dir" && mkdir -p "$dir"
        tar -xJf "$tb" -C "$dir" --strip-components=1 || die "unpack $1"
        touch "$dir/.verified-$2"
    fi
    echo "$dir/bin"
}
say "compilers (Arm GNU Toolchain, pinned)"
TC64="$(toolchain "$AARCH64_TC" "$AARCH64_TC_SHA256")"
TCM0="$(toolchain "$M0_TC" "$M0_TC_SHA256")"
CROSS="$TC64/aarch64-none-elf-"
M0CROSS="$TCM0/arm-none-eabi-"
"${CROSS}gcc" --version | head -1 | tee -a "$LOG"
"${M0CROSS}gcc" --version | head -1 | tee -a "$LOG"

# --- sources, by tag, refused unless they are the pinned commit ----------------------------------
fetch() {   # name repo tag-or-empty ref
    local dir="$WS/src-$1"
    if [ ! -d "$dir/.git" ]; then
        if [ -n "$3" ]; then
            git -c advice.detachedHead=false clone -q --depth 1 --branch "$3" "$2" "$dir" || die "clone $2 $3"
        else
            git init -q "$dir" && git -C "$dir" remote add origin "$2"
        fi
    fi
    if ! git -C "$dir" cat-file -e "$4^{commit}" 2>/dev/null; then
        git -C "$dir" fetch -q --depth 1 origin "$4" || die "fetch $1 $4"
    fi
    if [ -n "$3" ]; then
        git -C "$dir" fetch -q --depth 1 origin "refs/tags/$3:refs/tags/$3" 2>/dev/null || true
        local got
        got="$(git -C "$dir" rev-parse "refs/tags/$3^{commit}")"
        [ "$got" = "$4" ] || die "$1: tag $3 is $got, not the pinned $4"
    fi
}
say "sources"
fetch u-boot "$UBOOT_REPO" "$UBOOT_TAG" "$UBOOT_REF"
fetch tf-a "$TFA_REPO" "$TFA_TAG" "$TFA_REF"
fetch rkdeveloptool "$RKDEVELOPTOOL_REPO" "" "$RKDEVELOPTOOL_REF"
SOURCE_DATE_EPOCH="$(git -C "$WS/src-u-boot" show -s --format=%ct "$UBOOT_REF")"
export SOURCE_DATE_EPOCH TZ=UTC LC_ALL=C
STAMP="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%d %H:%M:%S')"
echo "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH ($STAMP UTC)" | tee -a "$LOG"

# --- python for binman (pyelftools) and pylibfdt (swig): pinned wheels, private to this build ----
say "python tools (pinned wheels)"
wheel() {   # url sha256 -> unpacked into $B/py
    local f="$WS/wheels/$(basename "$1")"
    mkdir -p "$WS/wheels"
    [ -f "$f" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$2" ] || curl -fsSL -o "$f" "$1" || die "fetch $1"
    [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$2" ] || die "$(basename "$1") is not sha256 $2"
    python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$f" "$B/py"
}
wheel "$PYELFTOOLS_WHEEL" "$PYELFTOOLS_SHA256"
wheel "$SWIG_WHEEL" "$SWIG_SHA256"
chmod +x "$B/py/swig/data/bin/swig"
mkdir -p "$B/bin"
SWIG_LIB_DIR="$(ls -d "$B"/py/swig/data/share/swig/*/ | head -1)"
printf '#!/bin/sh\nSWIG_LIB=%s exec %s "$@"\n' "$SWIG_LIB_DIR" "$B/py/swig/data/bin/swig" > "$B/bin/swig"
chmod +x "$B/bin/swig"
export PATH="$B/bin:$PATH" PYTHONPATH="$B/py${PYTHONPATH:+:$PYTHONPATH}"
python3 -c 'import elftools' || die "pyelftools does not import"

# --- TF-A BL31 with the M0 PMU firmware ------------------------------------------------------------
say "TF-A $TFA_TAG PLAT=rk3399 bl31, LOG_LEVEL=$TFA_LOG_LEVEL, M0 by arm-none-eabi"
mkdir -p "$B/tf-a"
git -C "$WS/src-tf-a" archive "$TFA_REF" | tar -x -C "$B/tf-a"
make -C "$B/tf-a" -j"$JOBS" CROSS_COMPILE="$CROSS" M0_CROSS_COMPILE="$M0CROSS" PLAT=rk3399 \
    LOG_LEVEL="$TFA_LOG_LEVEL" BUILD_STRING="$TFA_TAG" BUILD_MESSAGE_TIMESTAMP="\"$STAMP\"" \
    bl31 >>"$LOG" 2>&1 || { tail -40 "$LOG"; die "TF-A build failed"; }
BL31="$B/tf-a/build/rk3399/release/bl31/bl31.elf"
[ -s "$BL31" ] || die "no bl31.elf"
[ -s "$B/tf-a/build/rk3399/release/m0/rk3399m0.bin" ] || die "no M0 firmware (rk3399m0.bin)"
mkdir -p "$OUT/tf-a"
cp "$BL31" "$B/tf-a/build/rk3399/release/m0/rk3399m0.bin" "$B/tf-a/build/rk3399/release/m0/rk3399m0pmu.bin" \
   "$OUT/tf-a/"

# --- U-Boot: the pinned tree + patches/0001-0017 ---------------------------------------------------
say "U-Boot $UBOOT_TAG + patches"
mkdir -p "$B/u-boot"
git -C "$WS/src-u-boot" archive "$UBOOT_REF" | tar -x -C "$B/u-boot"
for p in "$HERE"/patches/*.patch; do
    (cd "$B/u-boot" && git apply --check "$p" && git apply "$p") || die "patch $(basename "$p") does not apply"
    echo "applied $(basename "$p")" | tee -a "$LOG"
done

for fam in $FAMILIES; do
    O="$B/build-$fam"
    say "U-Boot nova-$fam-rk3399 + nova-mainline.config"
    printf 'CONFIG_NOVA_BUILD_ID="%s-%s-mainline"\n' "$BUILD_ID" "$fam" > "$B/id-$fam.config"
    case "${NOVA_RK3399_DDR_MHZ:-800}" in
        800) ;;
        856) echo 'CONFIG_NOVA_RK3399_DDR_856=y' >> "$B/id-$fam.config" ;;
        400) echo 'CONFIG_NOVA_RK3399_DDR_400=y' >> "$B/id-$fam.config" ;;
        *) die "NOVA_RK3399_DDR_MHZ is 800, 856 or 400, not ${NOVA_RK3399_DDR_MHZ}" ;;
    esac
    case "${NOVA_RK3399_TPL_TRACE:-0}" in
        0) ;;
        1) echo 'CONFIG_NOVA_RK3399_TPL_TRACE=y' >> "$B/id-$fam.config" ;;
        *) die "NOVA_RK3399_TPL_TRACE is 0 or 1, not ${NOVA_RK3399_TPL_TRACE}" ;;
    esac
    for o in $(echo "${NOVA_RK3399_TPL_OPTS:-}" | tr ',' ' '); do
        case "$o" in
            trfc280)  echo 'CONFIG_NOVA_RK3399_LPDDR4_TRFC_280=y' >> "$B/id-$fam.config" ;;
            padspeed) echo 'CONFIG_NOVA_RK3399_LPDDR4_PAD_SPEED=y' >> "$B/id-$fam.config" ;;
            grfie)    echo 'CONFIG_NOVA_RK3399_DDR_GRF_IE_POLARITY=y' >> "$B/id-$fam.config" ;;
            *) die "NOVA_RK3399_TPL_OPTS: trfc280, padspeed or grfie, not $o" ;;
        esac
    done
    # The fragment merged over the defconfig FILE, then olddefconfig: every symbol neither names gets its default
    # and the implies of the mainline flavor. Every make reads /dev/null, so a Kconfig prompt fails instead of
    # hanging.
    mkdir -p "$O"
    (cd "$B/u-boot" && ./scripts/kconfig/merge_config.sh -m -O "$O" "configs/nova-$fam-rk3399_defconfig" \
        board/rigol/nova_rk3399/nova-mainline.config "$B/id-$fam.config") </dev/null >>"$LOG" 2>&1 \
        || die "$fam: merge_config"
    make -C "$B/u-boot" O="$O" CROSS_COMPILE="$CROSS" olddefconfig </dev/null >>"$LOG" 2>&1 || die "$fam: olddefconfig"
    # Every option the variant asked for is in the final .config: a symbol a Kconfig dependency drops would otherwise
    # build the default image under the variant's name.
    for want in $(grep -v '^CONFIG_NOVA_BUILD_ID=' "$B/id-$fam.config"); do
        grep -qx "$want" "$O/.config" || die "$fam: $want did not survive olddefconfig (a Kconfig dependency)"
    done
    variant="$(grep -v '^CONFIG_NOVA_BUILD_ID=' "$B/id-$fam.config" | tr '\n' ' ' || true)"
    echo "variant $fam: ${variant:-the default}" | tee -a "$LOG"
    # `prepare` alone first: with -j, the board .env's step can run before include/config.h exists. It also writes
    # TPL's and SPL's autoconf.mk, without which their sub-makes take U-Boot proper's rules.
    timeout 600 make -C "$B/u-boot" O="$O" CROSS_COMPILE="$CROSS" prepare </dev/null >>"$LOG" 2>&1 \
        || { tail -30 "$LOG"; die "$fam: U-Boot prepare failed (a Kconfig prompt, if it timed out)"; }
    make -C "$B/u-boot" O="$O" CROSS_COMPILE="$CROSS" BL31="$BL31" -j"$JOBS" </dev/null >>"$LOG" 2>&1 \
        || { tail -60 "$LOG"; die "$fam: U-Boot build failed"; }
    make -C "$B/u-boot" O="$O" CROSS_COMPILE="$CROSS" u-boot-initial-env </dev/null >>"$LOG" 2>&1 \
        || die "$fam: u-boot-initial-env"
    D="$OUT/$fam"
    mkdir -p "$D"
    for f in idbloader.img u-boot.itb u-boot-rockchip.bin u-boot-rockchip-usb471.bin u-boot-rockchip-usb472.bin \
             tpl/u-boot-tpl.bin spl/u-boot-spl.bin u-boot.bin u-boot u-boot.dtb u-boot-initial-env \
             spl/u-boot-spl.map tpl/u-boot-tpl.map u-boot.map; do
        [ -s "$O/$f" ] || die "$fam: no $f"
        cp "$O/$f" "$D/$(basename "$f")"
    done
    cp "$O/.config" "$D/final.config"
    "$O/tools/dumpimage" -l "$D/u-boot.itb" > "$D/u-boot.itb.txt"
    python3 "$HERE/check-uboot-rk3399.py" --build "$O" --family "$fam" --flavor mainline --cross "$CROSS" \
        --json "$D/uboot-gates.json" | tee -a "$LOG"
    [ "${PIPESTATUS[0]}" = 0 ] || die "$fam: U-Boot proper fails check-uboot-rk3399.py"
    python3 -I "$HERE/tools/mkloader.py" --471 "$D/u-boot-rockchip-usb471.bin" --472 "$D/u-boot-rockchip-usb472.bin" \
        -o "$D/rigol-$fam-maskrom-loader.bin" | tee -a "$LOG"
done

# --- rkdeveloptool's own parser reads each loader file, as `db` would --------------------------------
say "rkdeveloptool $RKDEVELOPTOOL_REF parses the loader files"
mkdir -p "$B/rkdev/stub" "$B/rkdev/src"
git -C "$WS/src-rkdeveloptool" archive "$RKDEVELOPTOOL_REF" | tar -x -C "$B/rkdev/src"
printf '/* stub: the loader parser needs no USB */\n#ifndef STUB_LIBUSB_H\n#define STUB_LIBUSB_H\n#include <stdint.h>\n#endif\n' > "$B/rkdev/stub/libusb.h"
g++ -O1 -w -I"$B/rkdev/stub" -I"$B/rkdev/src" -o "$B/rkdev/ldr_check" "$HERE/tools/ldr_check.cpp" \
    "$B/rkdev/src/RKImage.cpp" "$B/rkdev/src/RKBoot.cpp" "$B/rkdev/src/crc.cpp" >>"$LOG" 2>&1 \
    || { tail -30 "$LOG"; die "ldr_check does not build"; }
for fam in $FAMILIES; do
    mkdir -p "$OUT/$fam/ldr_check"
    "$B/rkdev/ldr_check" "$OUT/$fam/rigol-$fam-maskrom-loader.bin" "$OUT/$fam/ldr_check" | tee "$OUT/$fam/ldr_check.txt" \
        || die "$fam: rkdeveloptool refuses the loader file"
done

# --- the gates, then the manifest ----------------------------------------------------------------
say "gates"
python3 -I "$HERE/gates.py" --out "$OUT" --pins "$HERE/pins.env" --families "$FAMILIES" \
    --board "$B/u-boot/board/rigol/nova_rk3399" --lane-id "$BUILD_ID" \
    --cc gcc --bl31 "$OUT/tf-a/bl31.elf" --readelf "${CROSS}readelf" \
    --source-date-epoch "$SOURCE_DATE_EPOCH" --patches "$HERE/patches" --builder "$0" \
    --compilers "$("${CROSS}gcc" --version | head -1)" "$("${M0CROSS}gcc" --version | head -1)" \
    2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" = 0 ] || die "a gate failed"

# --- the default build against the binaries the RAM boots ran -------------------------------------
if [ -z "${NOVA_RK3399_DDR_MHZ:-}${NOVA_RK3399_TPL_OPTS:-}" ] && [ "${NOVA_RK3399_TPL_TRACE:-0}" = 0 ]; then
    n=0 bad=0
    while read -r sum rel; do
        case "$rel" in ''|'#'*) continue ;; esac
        n=$((n + 1))
        if [ ! -f "$OUT/$rel" ] || [ "$(sha256sum "$OUT/$rel" | cut -d' ' -f1)" != "$sum" ]; then
            echo "differs from expected.sha256: $rel" | tee -a "$LOG"; bad=$((bad + 1))
        fi
    done < <(grep -v '^#' "$HERE/expected.sha256")
    echo "build.sh: $((n - bad)) of $n binaries are the expected bytes (expected.sha256)" | tee -a "$LOG"
fi
say "done: $OUT (build id $BUILD_ID)"
