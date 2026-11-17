# rocksdb-prebuilt

Prebuilt static C++20 `librocksdb.a` archives compiled directly from upstream [`meta/rocksdb`](https://github.com/meta/rocksdb) release tags in both `ndebug` and `checked-status` variants.

## Automated Upstream Release Sync

**Actions -> Sync Upstream RocksDB Releases** (`.github/workflows/sync-upstream.yml`) polls [`meta/rocksdb`](https://github.com/meta/rocksdb) once daily after Meta's US Pacific workday (`17 4 * * 2-6` — Tue–Sat at `04:17 UTC`) and builds any published non-draft, non-prerelease version `>= 11.13.2` that is missing archives for any of the enabled target platforms:

- `x86_64-unknown-linux-gnu` (`linux_x64`)
- `aarch64-unknown-linux-gnu` (`linux_arm64`)
- `aarch64-apple-darwin` (`macos_arm64`)
- `x86_64-pc-windows-msvc` (`windows_x64`)

Completeness is checked per `(version, target)` pair (both `ndebug` and `checked-status` tarballs present and non-empty), so partial releases or transient CI failures self-heal on the next run without rebuilding already-uploaded platforms. Upstream 2-part tags such as `vX.Y` are normalized to canonical `vX.Y.0` releases and `rocksdb_vX_Y_0` namespaces.

You can also trigger **Sync Upstream RocksDB Releases** manually via `workflow_dispatch` with optional `floor_version`, `max_versions`, and `dry_run` inputs to inspect planned builds in the job summary before compiling.

## Manual GitHub Actions Build

Trigger **Actions -> Build Prebuilt RocksDB** (`workflow_dispatch`) to build a specific version on demand:

- `rocksdb_version`: Upstream RocksDB tag version (e.g. `11.8.1`, `11.12`, or `11.13.2`).
- `publish_release`: Whether to publish archives to GitHub Release `v<version>` (default `true`).
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
./scripts/build_prebuilt.sh --version 11.13.2
```

To build from an existing local RocksDB checkout instead of downloading from GitHub:

```bash
ROCKSDB_SOURCE_DIR=/path/to/rocksdb ./scripts/build_prebuilt.sh --version 11.13.2 --out-dir ./dist
```
