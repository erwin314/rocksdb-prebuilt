# rocksdb-prebuilt

Prebuilt static C++20 `librocksdb.a` archives compiled directly from upstream [`facebook/rocksdb`](https://github.com/facebook/rocksdb) release tags in both `ndebug` and `checked-status` variants.

## GitHub Actions Usage

Trigger **Actions -> Build Prebuilt RocksDB** (`workflow_dispatch`) with:

- `rocksdb_version`: Upstream RocksDB tag version (e.g. `11.8.1`, `11.13.2`).
- `publish_release`: Whether to publish archives to GitHub Release `v<rocksdb_version>` (default `true`).
- Platform toggles: `include_linux_x64`, `include_linux_arm64`, `include_macos_arm64`, `include_windows_x64`.

To add a new platform later to an existing version (e.g. macOS Apple Silicon for an already-released version), re-run the workflow with the same `rocksdb_version` and only the new platform checked. Existing release assets are preserved untouched.

## Release URLs & Archive Layout

Public tarballs are published at:

```text
https://github.com/erwin314/rocksdb-prebuilt/releases/download/v<version>/rocksdb-v<version>-<target>-<ndebug|checked-status>.tar.gz
```

Each tarball contains:

- `build-info.env` (`rocksdb_version`, `namespace`, `target`, `status_abi`, `cxx_defines`, `cxx_flags`)
- `lib/librocksdb.a` (compiled with `-std=c++20`, RTTI enabled, `ROCKSDB_NAMESPACE=rocksdb_v<major>_<minor>_<patch>`, and `db/c.cc` omitted)
- `include/rocksdb/...` and `port/stack_trace.h`
- Upstream RocksDB licenses (`LICENSE.Apache`, `COPYING`, and `LICENSE.leveldb` when present)

Compression (`lz4` and `zstd`) headers are consumed from `lz4-sys` and `zstd-sys`, so downstream Rust builds link against Cargo's compression crates.

## Building Locally

```bash
./scripts/build_prebuilt.sh --version 11.8.1
```

To build from an existing local RocksDB checkout instead of downloading from GitHub:

```bash
ROCKSDB_SOURCE_DIR=/path/to/rocksdb ./scripts/build_prebuilt.sh --version 11.8.1 --out-dir ./dist
```
