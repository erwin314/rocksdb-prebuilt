//! Build script compiling upstream RocksDB C++20 sources into static `librocksdb.a`
//! and emitting `build-info.env` alongside the archive.

use std::env;
use std::error::Error;
use std::ffi::OsStr;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};

fn emit(line: &str) -> Result<(), Box<dyn Error>> {
    let mut out = io::stdout().lock();
    writeln!(out, "{line}")?;
    Ok(())
}

fn main() -> Result<(), Box<dyn Error>> {
    for var in [
        "ROCKSDB_VERSION",
        "ROCKSDB_SOURCE_DIR",
        "CARGO_CFG_TARGET_FEATURE",
        "DEP_LZ4_INCLUDE",
        "DEP_ZSTD_INCLUDE",
    ] {
        emit(&format!("cargo::rerun-if-env-changed={var}"))?;
    }

    let version = parse_rocksdb_version()?;
    let rocksdb_dir = validated_source_dir(&version)?;
    let include_dir = rocksdb_dir.join("include");
    let src_mk = rocksdb_dir.join("src.mk");
    let build_version_in = rocksdb_dir.join("util/build_version.cc.in");
    let version_h = include_dir.join("rocksdb/version.h");

    for tracked in [&src_mk, &build_version_in, &version_h] {
        emit(&format!("cargo::rerun-if-changed={}", tracked.display()))?;
    }

    let out_dir = PathBuf::from(env::var_os("OUT_DIR").ok_or("OUT_DIR is unset")?);
    let target_triple = env::var("TARGET")?;
    let target_os = env::var("CARGO_CFG_TARGET_OS")?;
    let target_arch = env::var("CARGO_CFG_TARGET_ARCH")?;
    let target_env = env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    let pointer_width = env::var("CARGO_CFG_TARGET_POINTER_WIDTH")?;
    let checked_status = env::var_os("CARGO_FEATURE_CHECKED_STATUS").is_some();
    let features_raw = env::var("CARGO_CFG_TARGET_FEATURE").unwrap_or_default();

    let lz4_inc = env::var_os("DEP_LZ4_INCLUDE")
        .filter(|v| !v.is_empty())
        .ok_or("DEP_LZ4_INCLUDE is unset (lz4-sys did not export an include path)")?;
    let zstd_inc = env::var_os("DEP_ZSTD_INCLUDE")
        .filter(|v| !v.is_empty())
        .ok_or("DEP_ZSTD_INCLUDE is unset (zstd-sys did not export an include path)")?;

    let namespace = format!("rocksdb_v{}", version.replace('.', "_"));
    let status_abi = if checked_status {
        "checked-status"
    } else {
        "ndebug"
    };

    let cxx_defines = build_cxx_defines(
        &namespace,
        &target_os,
        &target_env,
        &pointer_width,
        checked_status,
    )?;
    let cxx_flags = build_cxx_flags(&target_os, &target_arch, &target_env, &features_raw);

    let lib_sources = parse_lib_sources(&src_mk, &target_os)?;
    let build_version_cc = generate_build_version(&build_version_in, &out_dir, &version)?;

    let mut build = cc::Build::new();
    build
        .cpp(true)
        .std("c++20")
        .warnings(false)
        .debug(false)
        .include(&rocksdb_dir)
        .include(&include_dir);

    if env::var("OPT_LEVEL").as_deref() == Ok("0") {
        build.opt_level(2);
    }

    add_include_paths(&mut build, &lz4_inc);
    add_include_paths(&mut build, &zstd_inc);

    for entry in &cxx_defines {
        if let Some((key, value)) = entry.split_once('=') {
            build.define(key, Some(value));
        } else {
            build.define(entry, None);
        }
    }

    if target_env == "msvc" {
        build.flag("-EHsc");
    } else {
        build.flag("-include").flag("cstdint");
    }
    for flag in &cxx_flags {
        build.flag_if_supported(flag);
    }

    for rel in lib_sources {
        build.file(rocksdb_dir.join(rel));
    }
    build.file(build_version_cc);

    build.compile("rocksdb");

    write_build_info(
        &out_dir,
        &version,
        &namespace,
        &target_triple,
        status_abi,
        &cxx_defines,
        &cxx_flags,
    )?;

    Ok(())
}

fn parse_rocksdb_version() -> Result<String, Box<dyn Error>> {
    let raw = env::var("ROCKSDB_VERSION")
        .map_err(|_| "ROCKSDB_VERSION env var is required (e.g. ROCKSDB_VERSION=11.8.1)")?;
    let trimmed = raw.trim();
    let bare = trimmed.strip_prefix('v').unwrap_or(trimmed);
    let mut parts = bare.split('.');
    let Some(major_str) = parts.next() else {
        return Err(format!("Invalid ROCKSDB_VERSION '{raw}': expected MAJOR.MINOR.PATCH").into());
    };
    let Some(minor_str) = parts.next() else {
        return Err(format!("Invalid ROCKSDB_VERSION '{raw}': expected MAJOR.MINOR.PATCH").into());
    };
    let Some(patch_str) = parts.next() else {
        return Err(format!("Invalid ROCKSDB_VERSION '{raw}': expected MAJOR.MINOR.PATCH").into());
    };
    if parts.next().is_some()
        || [major_str, minor_str, patch_str]
            .into_iter()
            .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(format!("Invalid ROCKSDB_VERSION '{raw}': expected MAJOR.MINOR.PATCH").into());
    }
    let major: u32 = major_str.parse()?;
    let minor: u32 = minor_str.parse()?;
    let patch: u32 = patch_str.parse()?;
    Ok(format!("{major}.{minor}.{patch}"))
}

fn validated_source_dir(expected_version: &str) -> Result<PathBuf, Box<dyn Error>> {
    let raw = env::var_os("ROCKSDB_SOURCE_DIR")
        .filter(|v| !v.is_empty())
        .ok_or("ROCKSDB_SOURCE_DIR env var is required and must point to a RocksDB source tree")?;
    let dir = PathBuf::from(raw);
    for rel in [
        "src.mk",
        "util/build_version.cc.in",
        "include/rocksdb/version.h",
        "port/stack_trace.h",
    ] {
        if !dir.join(rel).is_file() {
            return Err(format!(
                "ROCKSDB_SOURCE_DIR ({}) is missing required file '{rel}'",
                dir.display()
            )
            .into());
        }
    }

    let version_h_path = dir.join("include/rocksdb/version.h");
    let header_version = read_header_version(&version_h_path)?;
    if header_version != expected_version {
        return Err(format!(
            "RocksDB source version mismatch in {}: header reports {header_version}, expected {expected_version}",
            version_h_path.display()
        )
        .into());
    }

    Ok(dir)
}

fn read_header_version(version_h: &Path) -> Result<String, Box<dyn Error>> {
    let contents = fs::read_to_string(version_h)?;
    let mut major: Option<u32> = None;
    let mut minor: Option<u32> = None;
    let mut patch: Option<u32> = None;

    for raw_line in contents.lines() {
        let mut tokens = raw_line.split_whitespace();
        if tokens.next() != Some("#define") {
            continue;
        }
        let Some(macro_name) = tokens.next() else {
            continue;
        };
        let Some(raw_val) = tokens.next() else {
            continue;
        };
        let clean_val = raw_val
            .split("//")
            .next()
            .unwrap_or(raw_val)
            .split("/*")
            .next()
            .unwrap_or(raw_val);
        match macro_name {
            "ROCKSDB_MAJOR" => major = Some(clean_val.parse()?),
            "ROCKSDB_MINOR" => minor = Some(clean_val.parse()?),
            "ROCKSDB_PATCH" => patch = Some(clean_val.parse()?),
            _ => {}
        }
    }

    match (major, minor, patch) {
        (Some(maj), Some(min), Some(pat)) => Ok(format!("{maj}.{min}.{pat}")),
        _ => Err(format!(
            "Failed to parse ROCKSDB_MAJOR/MINOR/PATCH from {}",
            version_h.display()
        )
        .into()),
    }
}

fn add_include_paths(build: &mut cc::Build, raw: &OsStr) {
    let path = Path::new(raw);
    if path.exists() {
        build.include(path);
        return;
    }
    if let Some(s) = raw.to_str() {
        for part in s.split(';').filter(|p| !p.is_empty()) {
            build.include(part);
        }
    } else {
        build.include(path);
    }
}

fn build_cxx_defines(
    namespace: &str,
    target_os: &str,
    target_env: &str,
    pointer_width: &str,
    checked_status: bool,
) -> Result<Vec<String>, Box<dyn Error>> {
    let mut defines = Vec::new();
    for (name, value) in [
        ("ROCKSDB_NAMESPACE", namespace),
        ("LZ4", "1"),
        ("ZSTD", "1"),
        ("ROCKSDB_USE_RTTI", "1"),
        ("HAVE_ALIGNED_NEW", "1"),
    ] {
        defines.push(format!("{name}={value}"));
    }

    if checked_status {
        // ROCKSDB_ASSERT_STATUS_CHECKED relies on assert(...) remaining enabled.
        defines.push("ROCKSDB_ASSERT_STATUS_CHECKED=1".to_owned());
    } else {
        defines.push("NDEBUG=1".to_owned());
    }

    if target_env != "msvc" {
        defines.push("HAVE_UINT128_EXTENSION=1".to_owned());
    }

    let os_defines: &[&str] = match target_os {
        "linux" => &[
            "OS_LINUX",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
            "ROCKSDB_SCHED_GETCPU_PRESENT",
            "ROCKSDB_AUXV_GETAUXVAL_PRESENT",
            "ROCKSDB_FALLOCATE_PRESENT",
            "ROCKSDB_RANGESYNC_PRESENT",
        ],
        "macos" => &[
            "OS_MACOSX",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "ios" => &[
            "OS_MACOSX",
            "IOS_CROSS_COMPILE",
            "NIOSTATS_CONTEXT",
            "NPERF_CONTEXT",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "freebsd" => &[
            "OS_FREEBSD",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "openbsd" => &[
            "OS_OPENBSD",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "netbsd" => &[
            "OS_NETBSD",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "dragonfly" => &[
            "OS_DRAGONFLYBSD",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "android" => &[
            "OS_ANDROID",
            "ROCKSDB_PLATFORM_POSIX",
            "ROCKSDB_LIB_IO_POSIX",
        ],
        "windows" => &[
            "OS_WIN",
            "WIN32",
            "WIN64",
            "NOMINMAX",
            "ROCKSDB_WINDOWS_UTF8_FILENAMES",
        ],
        other => {
            return Err(format!("Unsupported target OS: {other}").into());
        }
    };

    for &name in os_defines {
        defines.push(format!("{name}=1"));
    }

    if target_os == "windows" && target_env == "gnu" {
        defines.push("_POSIX_C_SOURCE=1".to_owned());
        defines.push("_WIN32_WINNT=_WIN32_WINNT_VISTA".to_owned());
    }

    if pointer_width != "64" && target_os != "windows" {
        defines.push("_FILE_OFFSET_BITS=64".to_owned());
        defines.push("_LARGEFILE64_SOURCE=1".to_owned());
    }

    Ok(defines)
}

fn build_cxx_flags(
    target_os: &str,
    target_arch: &str,
    target_env: &str,
    features_raw: &str,
) -> Vec<String> {
    let mut flags = Vec::new();

    if target_env != "msvc" {
        for flag in [
            "-fno-omit-frame-pointer",
            "-fvisibility=hidden",
            "-fvisibility-inlines-hidden",
        ] {
            flags.push(flag.to_owned());
        }
        if matches!(
            target_os,
            "linux" | "freebsd" | "netbsd" | "openbsd" | "dragonfly"
        ) {
            flags.push("-fno-gnu-unique".to_owned());
        }
    }

    if target_arch == "x86_64" {
        let features: Vec<&str> = features_raw
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();

        // Default to upstream RocksDB's recommended portable x86_64 baseline
        // (`PORTABLE=haswell` / `/arch:AVX2`: SSE4.2 + PCLMUL + POPCNT + AVX2 +
        // BMI1/BMI2 + LZCNT), while still forwarding explicit AVX-512 target
        // features. End-user `CXXFLAGS` are appended later by `cc::Build` and
        // can still override `-march=` / `/arch:`.
        if target_env == "msvc" {
            if features.contains(&"avx512f") {
                flags.push("/arch:AVX512".to_owned());
            } else {
                flags.push("/arch:AVX2".to_owned());
            }
        } else {
            flags.push("-march=haswell".to_owned());
            for (feat, flag) in [
                ("avx512f", "-mavx512f"),
                ("avx512vl", "-mavx512vl"),
                ("avx512bw", "-mavx512bw"),
            ] {
                if features.contains(&feat) {
                    flags.push(flag.to_owned());
                }
            }
        }
    } else if target_arch == "aarch64"
        && target_env != "msvc"
        && !matches!(target_os, "macos" | "ios")
    {
        // Enable ARMv8 CRC32 and Crypto (PMULL) intrinsics for util/crc32c_arm64.cc,
        // which guards both behind runtime AT_HWCAP / sysctl checks. Skip on Apple
        // targets where Clang already defaults to apple-m1 (ARMv8.5-A + LSE + CRC + Crypto)
        // and passing -march=armv8-a... would downgrade LSE hardware atomics.
        flags.push("-march=armv8-a+crc+crypto".to_owned());
    }

    flags
}

fn parse_lib_sources(src_mk: &Path, target_os: &str) -> Result<Vec<String>, Box<dyn Error>> {
    let contents = fs::read_to_string(src_mk)?;
    let mut in_lib_sources = false;
    let mut sources = Vec::new();

    for raw_line in contents.lines() {
        let line = raw_line.trim_end_matches('\r');
        if !in_lib_sources {
            if let Some(rest) = line.strip_prefix("LIB_SOURCES")
                && let Some(after_eq) = rest.trim_start().strip_prefix('=')
            {
                in_lib_sources = true;
                if !push_source_tokens(after_eq, &mut sources, target_os) {
                    break;
                }
            }
            continue;
        }
        if !push_source_tokens(line, &mut sources, target_os) {
            break;
        }
    }

    if sources.is_empty() {
        return Err(format!("Failed to parse LIB_SOURCES from {}", src_mk.display()).into());
    }
    Ok(sources)
}

fn push_source_tokens(line: &str, sources: &mut Vec<String>, target_os: &str) -> bool {
    let trimmed = line.trim();
    let (body, has_continuation) = match trimmed.strip_suffix('\\') {
        Some(before) => (before, true),
        None => (trimmed, false),
    };

    for token in body.split_whitespace() {
        if token.starts_with('#') {
            break;
        }
        if matches!(token, "db/c.cc" | "util/build_version.cc") {
            continue;
        }
        if target_os != "windows" && token.starts_with("port/win/") {
            continue;
        }
        if target_os == "windows"
            && matches!(
                token,
                "port/port_posix.cc" | "env/env_posix.cc" | "env/fs_posix.cc" | "env/io_posix.cc"
            )
        {
            continue;
        }
        sources.push(token.to_owned());
    }

    has_continuation
}

fn generate_build_version(
    template_path: &Path,
    out_dir: &Path,
    version: &str,
) -> Result<PathBuf, Box<dyn Error>> {
    let template = fs::read_to_string(template_path)?;
    let git_tag = format!("v{version}");
    let rendered = template
        .replace("@GIT_SHA@", "0000000000000000000000000000000000000000")
        .replace("@GIT_TAG@", &git_tag)
        .replace("@GIT_MOD@", "0")
        .replace("@GIT_DATE@", "2026-01-01 00:00:00")
        .replace("@BUILD_DATE@", "2026-01-01 00:00:00")
        .replace("@ROCKSDB_PLUGIN_EXTERNS@", "")
        .replace("@ROCKSDB_PLUGIN_BUILTINS@", "");

    let dest = out_dir.join("build_version.cc");
    write_if_changed(&dest, &rendered)?;
    Ok(dest)
}

fn write_build_info(
    out_dir: &Path,
    version: &str,
    namespace: &str,
    target_triple: &str,
    status_abi: &str,
    cxx_defines: &[String],
    cxx_flags: &[String],
) -> Result<(), Box<dyn Error>> {
    let content = format!(
        "format=1\n\
         rocksdb_version={version}\n\
         namespace={namespace}\n\
         target={target_triple}\n\
         status_abi={status_abi}\n\
         cxx_defines={}\n\
         cxx_flags={}\n",
        cxx_defines.join(";"),
        cxx_flags.join(";"),
    );
    write_if_changed(&out_dir.join("build-info.env"), &content)
}

fn write_if_changed(path: &Path, content: &str) -> Result<(), Box<dyn Error>> {
    let unchanged = fs::read_to_string(path).is_ok_and(|existing| existing == content);
    if !unchanged {
        fs::write(path, content)?;
    }
    Ok(())
}
