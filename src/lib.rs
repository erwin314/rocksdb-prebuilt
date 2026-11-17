#![forbid(unsafe_code)]
//! Marker crate whose `build.rs` compiles `librocksdb.a` against Cargo's `lz4-sys` and `zstd-sys` headers.

use lz4_sys as _;
use zstd_sys as _;
