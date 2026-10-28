#!/usr/bin/env bash
# Rebuild the RK3399 boot chain of this release (U-Boot, TF-A, and the chainload recovery image) from this
# directory alone: nothing is fetched.
#   bash build.sh [--out DIR] [--work DIR] [--lanes "first-stage uboot-proper"] [--families "dho900 mho900"]
#                 [--toolchains DIR] [--stage-b FAMILY=stage-b.img ...] [--check]
# src/ holds each component's complete source tree with our patches applied (FACTS.env names them); each build runs
# in a copy of its tree under WORKDIR, so src/ stays as published. BUILDING.txt names the compilers and host tools.
# --stage-b gives the release's stage-B image of a family: with it, install/ rebuilds that family's
# recovery-uboot-chain.img. Out: OUTDIR/<lane>/..., the paths of expected.sha256, which the end compares
# (--check: exit 1 on a difference).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/FACTS.env"
OUT="$HERE/out" W="$HERE/work" TOOLCHAINS="" CHECK=0 WANT_LANES="$LANES" WANT_FAMILIES=""
declare -A STAGE_B=()
while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --work) W="$2"; shift 2 ;;
        --lanes) WANT_LANES="$2"; shift 2 ;;
        --families) WANT_FAMILIES="$2"; shift 2 ;;
        --toolchains) TOOLCHAINS="$2"; shift 2 ;;
        --stage-b) STAGE_B["${2%%=*}"]="${2#*=}"; shift 2 ;;
        --check) CHECK=1; shift ;;
        *) echo "build.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
die() { echo "build.sh: $*" >&2; exit 1; }
JOBS="${JOBS:-$(nproc)}"
export SOURCE_DATE_EPOCH TZ=UTC LC_ALL=C
mkdir -p "$OUT" "$W"
OUT="$(cd "$OUT" && pwd)"; W="$(cd "$W" && pwd)"
for l in $WANT_LANES; do case " $LANES " in *" $l "*) ;; *) die "no lane $l here (this bundle has: $LANES)" ;; esac; done
for t in make gcc bison flex python3 timeout sha256sum cp awk; do command -v "$t" >/dev/null || die "no $t (BUILDING.txt)"; done
# U-Boot proper's build runs awk scripts, and with mawk in place of GNU awk its u-boot.bin comes out different
awk --version 2>/dev/null | head -1 | grep -q '^GNU Awk' || echo "build.sh: WARNING: awk is not GNU awk" \
    "(install gawk, BUILDING.txt): U-Boot proper's u-boot.bin will differ" >&2

tree() {        # tree-dir -> a fresh copy of src/<tree> in WORKDIR
    rm -rf "${W:?}/$1"
    cp -a "$HERE/src/$1" "$W/$1"
    echo "$W/$1"
}
same_cc() {     # compiler wanted label
    local got
    got="$("$1" --version 2>/dev/null | head -1)" || die "no $1"
    [ "$got" = "$2" ] || echo "build.sh: WARNING: $3 is \"$got\", the release was built with \"$2\":" \
        "the binaries will differ" >&2
}
python_tools() {
    python3 -c 'import setuptools' 2>/dev/null || die "python3 has no setuptools (U-Boot builds pylibfdt with it)"
    python3 -c 'import sysconfig, os, sys; sys.exit(not os.path.exists(os.path.join(sysconfig.get_paths()["include"], "Python.h")))' \
        || die "no Python.h (python3's development headers: pylibfdt)"
    python3 -c 'import elftools' 2>/dev/null || die "python3 cannot import elftools (pyelftools, for binman: BUILDING.txt)"
    command -v swig >/dev/null || die "no swig (pylibfdt: BUILDING.txt)"
}

first_stage() {
    local U T tc64 tcm0 stamp bl31 fam O D f
    U="$(tree "$FIRST_STAGE_UBOOT")"; T="$(tree "$FIRST_STAGE_TFA")"
    tc64="${CROSS_COMPILE:-aarch64-none-elf-}"; tcm0="${M0_CROSS_COMPILE:-arm-none-eabi-}"
    if [ -n "$TOOLCHAINS" ]; then
        tc64="$TOOLCHAINS/$FIRST_STAGE_AARCH64_TC/bin/aarch64-none-elf-"
        tcm0="$TOOLCHAINS/$FIRST_STAGE_M0_TC/bin/arm-none-eabi-"
    fi
    same_cc "${tc64}gcc" "$FIRST_STAGE_CC" "the aarch64 compiler"
    same_cc "${tcm0}gcc" "$FIRST_STAGE_M0_CC" "the Cortex-M0 compiler"
    stamp="$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%d %H:%M:%S')"
    echo "== TF-A $TFA_TAG PLAT=rk3399 bl31"
    make -C "$T" -j"$JOBS" CROSS_COMPILE="$tc64" M0_CROSS_COMPILE="$tcm0" PLAT=rk3399 \
        LOG_LEVEL="$FIRST_STAGE_TFA_LOG_LEVEL" BUILD_STRING="$TFA_TAG" BUILD_MESSAGE_TIMESTAMP="\"$stamp\"" bl31
    bl31="$T/build/rk3399/release/bl31/bl31.elf"
    mkdir -p "$OUT/first-stage/tf-a"
    cp "$bl31" "$T/build/rk3399/release/m0/rk3399m0.bin" "$T/build/rk3399/release/m0/rk3399m0pmu.bin" \
        "$OUT/first-stage/tf-a/"
    for fam in ${WANT_FAMILIES:-$FIRST_STAGE_FAMILIES}; do
        echo "== U-Boot rigol-$fam-rk3399 (TPL, SPL, U-Boot, the FIT with BL31)"
        O="$W/first-stage-build-$fam"
        rm -rf "$O"
        make -C "$U" O="$O" CROSS_COMPILE="$tc64" "rigol-$fam-rk3399_defconfig"
        make -C "$U" O="$O" CROSS_COMPILE="$tc64" BL31="$bl31" -j"$JOBS"
        D="$OUT/first-stage/$fam"
        mkdir -p "$D"
        for f in idbloader.img u-boot.itb u-boot-rockchip.bin u-boot-rockchip-usb471.bin u-boot-rockchip-usb472.bin \
                 u-boot.bin u-boot.dtb spl/u-boot-spl.bin tpl/u-boot-tpl.bin; do
            cp "$O/$f" "$D/"
        done
    done
}

uboot_proper() {
    local U cross fam flavor O D lz4 sb
    U="$(tree "$UBOOT_PROPER_UBOOT")"
    cross="${CROSS_COMPILE:-$UBOOT_PROPER_CROSS_COMPILE}"
    same_cc "${cross}gcc" "$UBOOT_PROPER_CC" "the aarch64 compiler"
    [ -z "$UBOOT_PROPER_LD" ] || same_cc "${cross}ld" "$UBOOT_PROPER_LD" "the linker"
    export KBUILD_BUILD_USER="$UBOOT_PROPER_KBUILD_BUILD_USER" KBUILD_BUILD_HOST="$UBOOT_PROPER_KBUILD_BUILD_HOST" LOCALVERSION=
    for fam in ${WANT_FAMILIES:-$UBOOT_PROPER_FAMILIES}; do
        for flavor in $UBOOT_PROPER_FLAVORS; do
            echo "== U-Boot proper nova-$fam-rk3399 + nova-$flavor.config"
            O="$W/uboot-proper-build-$fam-$flavor"
            rm -rf "$O"
            printf 'CONFIG_NOVA_BUILD_ID="%s-%s-%s"\n' "$UBOOT_PROPER_BUILD_ID" "$fam" "$flavor" > "$W/id-$fam-$flavor.config"
            make -C "$U" O="$O" CROSS_COMPILE="$cross" "nova-$fam-rk3399_defconfig" </dev/null
            (cd "$U" && ./scripts/kconfig/merge_config.sh -m -O "$O" "$O/.config" \
                "board/rigol/nova_rk3399/nova-$flavor.config" "$W/id-$fam-$flavor.config") </dev/null
            make -C "$U" O="$O" CROSS_COMPILE="$cross" olddefconfig </dev/null
            timeout 120 make -C "$U" O="$O" CROSS_COMPILE="$cross" syncconfig </dev/null
            make -C "$U" O="$O" CROSS_COMPILE="$cross" prepare </dev/null
            make -C "$U" O="$O" CROSS_COMPILE="$cross" -j"$JOBS" u-boot.bin </dev/null
            D="$OUT/uboot-proper/$fam/$flavor"
            mkdir -p "$D"
            cp "$O/u-boot.bin" "$D/"
            cp "$O/dts/dt.dtb" "$D/u-boot.dtb"
        done
    done
    [ -n "${CHAIN_FAMILIES:-}" ] || return 0
    # the chainload recovery image: install/ packs the chainload u-boot.bin and the release's stage-B image, the
    # kernel compressed by the lz4 release in src/ (lz4 -9)
    lz4="$(tree "$LZ4_TREE")"
    make -C "$lz4/programs" -j"$JOBS" lz4 >/dev/null
    for fam in ${WANT_FAMILIES:-$CHAIN_FAMILIES}; do
        case " $CHAIN_FAMILIES " in *" $fam "*) ;; *) continue ;; esac
        sb="${STAGE_B[$fam]:-}"
        if [ -z "$sb" ]; then echo "build.sh: no --stage-b $fam=FILE: the $fam recovery image is not rebuilt" >&2; continue; fi
        want="CHAIN_STAGE_B_SHA256_$fam"
        [ "$(sha256sum "$sb" | cut -d' ' -f1)" = "${!want}" ] \
            || echo "build.sh: WARNING: $sb is not the stage-B image this release's $fam recovery image carries" >&2
        echo "== recovery-uboot-chain.img ($fam)"
        mkdir -p "$OUT/uboot-proper/$fam/chain"
        python3 "$HERE/install/tools/boot/dho900_uboot_chain.py" build --stage-b "$sb" \
            --uboot "$OUT/uboot-proper/$fam/chainload/u-boot.bin" --parm-cmdline "$HERE/install/parm-cmdline.txt" \
            --lz4 "$lz4/programs/lz4" --out "$OUT/uboot-proper/$fam/chain/recovery-uboot-chain.img" >/dev/null
    done
}

python_tools
for l in $WANT_LANES; do
    case "$l" in first-stage) first_stage ;; uboot-proper) uboot_proper ;; esac
done

# the outputs against the shipped binaries (expected.sha256)
n=0 bad=0 skipped=0
while read -r sum rel; do
    lane="${rel%%/*}"; rest="${rel#*/}"; fam="${rest%%/*}"
    case " $WANT_LANES " in *" $lane "*) ;; *) continue ;; esac
    if [ -n "$WANT_FAMILIES" ] && [ "$fam" != tf-a ]; then
        case " $WANT_FAMILIES " in *" $fam "*) ;; *) continue ;; esac
    fi
    # a recovery image is rebuilt only from its family's stage-B image (--stage-b): without one it is not compared
    case "$rel" in */chain/*)
        if [ -z "${STAGE_B[$fam]:-}" ]; then
            echo "not rebuilt (it needs the release's stage-B image, --stage-b $fam=FILE): $rel"
            skipped=$((skipped + 1)); continue
        fi ;;
    esac
    n=$((n + 1))
    if [ ! -f "$OUT/$rel" ] || [ "$(sha256sum "$OUT/$rel" | cut -d' ' -f1)" != "$sum" ]; then
        echo "differs from the shipped binary: $rel"; bad=$((bad + 1))
    fi
done < "$HERE/expected.sha256"
echo "build.sh: $((n - bad)) of $n binaries are the shipped bytes ($OUT)" \
    "$([ "$skipped" = 0 ] || echo "($skipped recovery image(s) not rebuilt: no --stage-b)")"
[ "$CHECK" = 0 ] || [ "$bad" = 0 ]
