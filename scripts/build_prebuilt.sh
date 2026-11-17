#!/usr/bin/env bash
set -euo pipefail

# Builds prebuilt upstream RocksDB static library tarballs in both `ndebug` and
# `checked-status` ABI variants.
#
# Usage:
#   ./scripts/build_prebuilt.sh --version <X.Y.Z> [--target <TRIPLE>] [--out-dir <DIR>]

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

to_unix_path() {
    local p="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "${p}"
    else
        printf '%s' "${p}"
    fi
}

to_native_path() {
    local p="$1"
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "${p}"
    else
        printf '%s' "${p}"
    fi
}

usage() {
    cat <<'EOF'
Usage: build_prebuilt.sh --version <X.Y.Z> [--target <TRIPLE>] [--out-dir <DIR>]

Options:
  --version <X.Y.Z>   Upstream RocksDB version to build (e.g. 11.8.1 or v11.8.1)
  --target <TRIPLE>   Rust target triple (defaults to host target from rustc -vV)
  --out-dir <DIR>     Output directory for .tar.gz archives (defaults to ./dist)
  -h, --help          Show this help message
EOF
}

VERSION=""
TARGET=""
OUT_DIR="${REPO_DIR}/dist"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            [[ $# -ge 2 ]] || { echo "ERROR: --version requires an argument" >&2; exit 2; }
            VERSION="$2"
            shift 2
            ;;
        --version=*)
            VERSION="${1#*=}"
            shift
            ;;
        --target)
            [[ $# -ge 2 ]] || { echo "ERROR: --target requires an argument" >&2; exit 2; }
            TARGET="$2"
            shift 2
            ;;
        --target=*)
            TARGET="${1#*=}"
            shift
            ;;
        --out-dir)
            [[ $# -ge 2 ]] || { echo "ERROR: --out-dir requires an argument" >&2; exit 2; }
            OUT_DIR="$2"
            shift 2
            ;;
        --out-dir=*)
            OUT_DIR="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

VERSION="${VERSION#v}"
if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: --version <X.Y.Z> is required (got '${VERSION}')" >&2
    usage >&2
    exit 2
fi

if [[ -z "${TARGET}" ]]; then
    TARGET="$(rustc -vV | sed -n 's/^host: //p' | tr -d '\r')"
fi
if [[ -z "${TARGET}" ]]; then
    echo "ERROR: failed to determine target triple; pass --target <TRIPLE>" >&2
    exit 1
fi

OUT_DIR="$(to_unix_path "${OUT_DIR}")"

read_header_version() {
    local version_h="$1"
    awk '
        $1 == "#define" && $2 == "ROCKSDB_MAJOR" { maj = $3 }
        $1 == "#define" && $2 == "ROCKSDB_MINOR" { min = $3 }
        $1 == "#define" && $2 == "ROCKSDB_PATCH" { pat = $3 }
        END {
            if (maj != "" && min != "" && pat != "") {
                gsub(/\r/, "", maj)
                gsub(/\r/, "", min)
                gsub(/\r/, "", pat)
                printf "%s.%s.%s\n", maj, min, pat
            }
        }
    ' "${version_h}"
}

verify_rocksdb_tree() {
    local dir="$1"
    local expected_version="$2"
    [[ -n "${dir}" && -d "${dir}" ]] || return 1
    for rel in \
        "src.mk" \
        "util/build_version.cc.in" \
        "table/two_level_iterator.cc" \
        "include/rocksdb/version.h" \
        "port/stack_trace.h" \
        "LICENSE.Apache" \
        "COPYING"; do
        [[ -f "${dir}/${rel}" ]] || return 1
    done
    local actual_version
    actual_version="$(read_header_version "${dir}/include/rocksdb/version.h")"
    [[ "${actual_version}" == "${expected_version}" ]]
}

ensure_rocksdb_source() {
    local version="$1"
    if [[ -n "${ROCKSDB_SOURCE_DIR:-}" ]]; then
        local explicit
        explicit="$(to_unix_path "${ROCKSDB_SOURCE_DIR}")"
        if [[ ! -d "${explicit}" ]]; then
            echo "ERROR: ROCKSDB_SOURCE_DIR does not exist: ${ROCKSDB_SOURCE_DIR}" >&2
            exit 1
        fi
        explicit="$(cd -- "${explicit}" && pwd)"
        if ! verify_rocksdb_tree "${explicit}" "${version}"; then
            local found="unknown"
            if [[ -f "${explicit}/include/rocksdb/version.h" ]]; then
                found="$(read_header_version "${explicit}/include/rocksdb/version.h" || true)"
            fi
            echo "ERROR: ROCKSDB_SOURCE_DIR (${explicit}) is invalid or version mismatch (found '${found}', expected '${version}')" >&2
            exit 1
        fi
        printf '%s\n' "${explicit}"
        return 0
    fi

    local cache_base="${ROCKSDB_CACHE_DIR:-${HOME}/.cache/rocksdb-prebuilt}"
    cache_base="$(to_unix_path "${cache_base}")"
    local src_slot="${cache_base}/src/v${version}"

    if verify_rocksdb_tree "${src_slot}" "${version}"; then
        printf '%s\n' "${src_slot}"
        return 0
    fi

    mkdir -p "${cache_base}/src"
    local fetch_tmp
    fetch_tmp="$(mktemp -d "${cache_base}/src/.fetch-v${version}.XXXXXX")"

    local tarball_url="https://github.com/facebook/rocksdb/archive/refs/tags/v${version}.tar.gz"
    local staged_tree=""
    echo "==> Fetching upstream RocksDB v${version}..." >&2
    if curl -fsSL --retry 3 --retry-delay 2 "${tarball_url}" -o "${fetch_tmp}/rocksdb.tar.gz" \
        && tar -xzf "${fetch_tmp}/rocksdb.tar.gz" -C "${fetch_tmp}" \
        && verify_rocksdb_tree "${fetch_tmp}/rocksdb-${version}" "${version}"; then
        staged_tree="${fetch_tmp}/rocksdb-${version}"
    else
        echo "Tarball fetch failed or mismatched; falling back to git clone v${version}..." >&2
        rm -rf -- "${fetch_tmp}/rocksdb-clone"
        git clone --depth 1 --branch "v${version}" "https://github.com/facebook/rocksdb.git" "${fetch_tmp}/rocksdb-clone" >&2
        rm -rf -- "${fetch_tmp}/rocksdb-clone/.git"
        if ! verify_rocksdb_tree "${fetch_tmp}/rocksdb-clone" "${version}"; then
            rm -rf -- "${fetch_tmp}"
            echo "ERROR: fetched RocksDB v${version} tree failed validation" >&2
            exit 1
        fi
        staged_tree="${fetch_tmp}/rocksdb-clone"
    fi

    rm -rf -- "${src_slot}"
    if ! mv "${staged_tree}" "${src_slot}" 2>/dev/null; then
        if ! verify_rocksdb_tree "${src_slot}" "${version}"; then
            rm -rf -- "${fetch_tmp}"
            echo "ERROR: failed to stage RocksDB v${version} source cache at ${src_slot}" >&2
            exit 1
        fi
    fi
    rm -rf -- "${fetch_tmp}"

    printf '%s\n' "${src_slot}"
}

SRC_DIR="$(ensure_rocksdb_source "${VERSION}")"
SRC_DIR_NATIVE="$(to_native_path "${SRC_DIR}")"
MANIFEST_NATIVE="$(to_native_path "${REPO_DIR}/Cargo.toml")"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rocksdb-prebuilt.XXXXXX")"
cleanup() {
    chmod -R u+w "${WORK_DIR}" 2>/dev/null || true
    rm -rf -- "${WORK_DIR}"
}
trap cleanup EXIT

mkdir -p "${OUT_DIR}"

for variant in ndebug checked-status; do
    VARIANT_TARGET="${WORK_DIR}/target-${variant}"
    VARIANT_TARGET_NATIVE="$(to_native_path "${VARIANT_TARGET}")"
    STAGE_DIR="${WORK_DIR}/stage-${variant}"
    EXTRA_ARGS=()
    if [[ "${variant}" == "checked-status" ]]; then
        EXTRA_ARGS+=(--features checked-status)
    fi

    echo "==> Compiling RocksDB v${VERSION} (${TARGET}, ${variant})..."
    ROCKSDB_VERSION="${VERSION}" \
    ROCKSDB_SOURCE_DIR="${SRC_DIR_NATIVE}" \
    CARGO_TARGET_DIR="${VARIANT_TARGET_NATIVE}" \
    cargo build \
        --manifest-path "${MANIFEST_NATIVE}" \
        --locked \
        --release \
        --target "${TARGET}" \
        ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

    OUT_FOUND=""
    for candidate in \
        "${VARIANT_TARGET}/${TARGET}/release/build"/rocksdb-prebuilt-builder-*/out \
        "${VARIANT_TARGET}/${TARGET}/release/build"/rocksdb-prebuilt-builder/*/out; do
        if [[ -f "${candidate}/build-info.env" ]] && { [[ -f "${candidate}/librocksdb.a" ]] || [[ -f "${candidate}/rocksdb.lib" ]]; }; then
            OUT_FOUND="${candidate}"
        fi
    done

    if [[ -z "${OUT_FOUND}" ]]; then
        echo "ERROR: failed to locate compiled archive and build-info.env in ${VARIANT_TARGET}/${TARGET}/release/build" >&2
        exit 1
    fi

    LIB_SRC="${OUT_FOUND}/librocksdb.a"
    if [[ ! -f "${LIB_SRC}" ]]; then
        LIB_SRC="${OUT_FOUND}/rocksdb.lib"
    fi

    rm -rf -- "${STAGE_DIR}"
    mkdir -p "${STAGE_DIR}/lib" "${STAGE_DIR}/include" "${STAGE_DIR}/port"
    cp "${OUT_FOUND}/build-info.env" "${STAGE_DIR}/build-info.env"
    cp "${LIB_SRC}" "${STAGE_DIR}/lib/librocksdb.a"
    cp -R "${SRC_DIR}/include/rocksdb" "${STAGE_DIR}/include/rocksdb"
    cp "${SRC_DIR}/port/stack_trace.h" "${STAGE_DIR}/port/stack_trace.h"
    cp "${SRC_DIR}/LICENSE.Apache" "${STAGE_DIR}/LICENSE.Apache"
    cp "${SRC_DIR}/COPYING" "${STAGE_DIR}/COPYING"

    TAR_ENTRIES=(build-info.env lib include port LICENSE.Apache COPYING)
    if [[ -f "${SRC_DIR}/LICENSE.leveldb" ]]; then
        cp "${SRC_DIR}/LICENSE.leveldb" "${STAGE_DIR}/LICENSE.leveldb"
        TAR_ENTRIES+=(LICENSE.leveldb)
    fi
    chmod -R u+w "${STAGE_DIR}"

    ARCHIVE_NAME="rocksdb-v${VERSION}-${TARGET}-${variant}.tar.gz"
    ARCHIVE_PATH="${OUT_DIR}/${ARCHIVE_NAME}"
    (
        cd -- "${STAGE_DIR}"
        tar -cf - "${TAR_ENTRIES[@]}" | gzip -n
    ) > "${ARCHIVE_PATH}"

    echo "==> Created ${ARCHIVE_PATH}"
done
