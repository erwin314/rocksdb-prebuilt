#!/usr/bin/env python3
"""Discover upstream meta/rocksdb releases missing prebuilt target archives."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Sequence
import urllib.error
import urllib.request

TARGET_RUNNERS: dict[str, str] = {
    "x86_64-unknown-linux-gnu": "ubuntu-24.04",
    "aarch64-unknown-linux-gnu": "ubuntu-24.04-arm",
    "aarch64-apple-darwin": "macos-15",
    "x86_64-pc-windows-msvc": "windows-2025",
}

DEFAULT_TARGETS: tuple[str, ...] = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
)

VARIANTS: tuple[str, ...] = ("ndebug", "checked-status")

_VERSION_RE = re.compile(r"^([0-9]+)\.([0-9]+)(?:\.([0-9]+))?$")
_TAG_RE = re.compile(r"^v([0-9]+)\.([0-9]+)(?:\.([0-9]+))?$")
_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

_TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class UpstreamRelease:
    """Canonical upstream release metadata."""

    semver: tuple[int, int, int]
    version: str
    release_tag: str
    upstream_tag: str


@dataclass(frozen=True)
class ReleasePlanRow:
    """Per-release planning status for summary reporting."""

    release: UpstreamRelease
    target_statuses: dict[str, str]
    deferred: bool


@dataclass(frozen=True)
class SyncPlan:
    """Computed release and matrix plan for missing prebuilt archives."""

    has_work: bool
    releases: list[dict[str, str]]
    matrix: list[dict[str, str]]
    rows: list[ReleasePlanRow]


def parse_floor_version(raw: str) -> tuple[int, int, int]:
    """Parse X.Y or X.Y.Z (optional 'v' prefix) into a 3-part semver tuple."""
    cleaned = raw.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    match = _VERSION_RE.fullmatch(cleaned)
    if not match:
        raise ValueError(
            f"Invalid floor version {raw!r}: expected MAJOR.MINOR.PATCH or MAJOR.MINOR"
        )
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return (major, minor, patch)


def parse_upstream_release_tag(
    tag_name: str,
) -> tuple[tuple[int, int, int], str, str] | None:
    """Parse an upstream tag like v11.13.2 or v11.12 into (semver, canonical, tag)."""
    match = _TAG_RE.fullmatch(tag_name.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    semver = (major, minor, patch)
    canonical = f"{major}.{minor}.{patch}"
    return (semver, canonical, tag_name.strip())


def collect_upstream_releases(
    raw_releases: Sequence[dict[str, Any]],
    floor: tuple[int, int, int],
) -> list[UpstreamRelease]:
    """Filter non-draft, non-prerelease upstream releases >= floor, sorted ascending."""
    by_version: dict[str, UpstreamRelease] = {}
    for entry in raw_releases:
        if entry.get("draft") or entry.get("prerelease"):
            continue
        tag_name = entry.get("tag_name")
        if not isinstance(tag_name, str):
            continue
        parsed = parse_upstream_release_tag(tag_name)
        if parsed is None:
            continue
        semver, version, upstream_tag = parsed
        if semver < floor:
            continue
        release_tag = f"v{version}"
        candidate = UpstreamRelease(
            semver=semver,
            version=version,
            release_tag=release_tag,
            upstream_tag=upstream_tag,
        )
        existing = by_version.get(version)
        # Prefer exact 3-part upstream tag (vX.Y.0) over 2-part alias (vX.Y).
        if existing is None or (
            existing.upstream_tag != existing.release_tag
            and candidate.upstream_tag == candidate.release_tag
        ):
            by_version[version] = candidate

    return sorted(by_version.values(), key=lambda rel: rel.semver)


def index_uploaded_assets(
    raw_prebuilt_releases: Sequence[dict[str, Any]],
) -> dict[str, set[str]]:
    """Map published release tag -> uploaded non-empty asset filenames."""
    assets_by_tag: dict[str, set[str]] = {}
    for entry in raw_prebuilt_releases:
        if entry.get("draft"):
            continue
        tag_name = entry.get("tag_name")
        if not isinstance(tag_name, str) or not tag_name:
            continue
        bucket = assets_by_tag.setdefault(tag_name, set())
        for asset in entry.get("assets") or ():
            name = asset.get("name")
            state = asset.get("state")
            size = int(asset.get("size") or 0)
            if isinstance(name, str) and state == "uploaded" and size > 0:
                bucket.add(name)
    return assets_by_tag


def expected_archive_names(version: str, target: str) -> tuple[str, ...]:
    """Return the required tarball filenames for a (version, target) build."""
    return tuple(
        f"rocksdb-v{version}-{target}-{variant}.tar.gz" for variant in VARIANTS
    )


def plan_missing_builds(
    upstream_releases: Sequence[UpstreamRelease],
    uploaded_assets_by_tag: dict[str, set[str]],
    targets: Sequence[str],
    max_versions: int,
) -> SyncPlan:
    """Compute missing (version, target) builds capped at max_versions incomplete releases."""
    if max_versions < 1:
        raise ValueError(f"--max-versions must be >= 1 (got {max_versions})")
    if not targets:
        raise ValueError("At least one target must be specified")

    ordered_targets: list[str] = []
    seen_targets: set[str] = set()
    for target in targets:
        if target not in TARGET_RUNNERS:
            supported = ", ".join(TARGET_RUNNERS)
            raise ValueError(
                f"Unsupported target {target!r}; supported targets: {supported}"
            )
        if target not in seen_targets:
            seen_targets.add(target)
            ordered_targets.append(target)

    releases: list[dict[str, str]] = []
    matrix: list[dict[str, str]] = []
    rows: list[ReleasePlanRow] = []
    queued_versions = 0

    for rel in upstream_releases:
        uploaded = uploaded_assets_by_tag.get(rel.release_tag, set())
        missing_targets = [
            target
            for target in ordered_targets
            if not all(
                archive in uploaded
                for archive in expected_archive_names(rel.version, target)
            )
        ]

        if not missing_targets:
            rows.append(
                ReleasePlanRow(
                    release=rel,
                    target_statuses={t: "present" for t in ordered_targets},
                    deferred=False,
                )
            )
            continue

        missing_set = set(missing_targets)
        if queued_versions < max_versions:
            queued_versions += 1
            releases.append(
                {
                    "version": rel.version,
                    "release_tag": rel.release_tag,
                    "upstream_tag": rel.upstream_tag,
                }
            )
            for target in missing_targets:
                matrix.append(
                    {
                        "version": rel.version,
                        "release_tag": rel.release_tag,
                        "upstream_tag": rel.upstream_tag,
                        "target": target,
                        "os": TARGET_RUNNERS[target],
                    }
                )
            statuses = {
                t: ("queued" if t in missing_set else "present")
                for t in ordered_targets
            }
            rows.append(
                ReleasePlanRow(
                    release=rel,
                    target_statuses=statuses,
                    deferred=False,
                )
            )
        else:
            statuses = {
                t: ("deferred" if t in missing_set else "present")
                for t in ordered_targets
            }
            rows.append(
                ReleasePlanRow(
                    release=rel,
                    target_statuses=statuses,
                    deferred=True,
                )
            )

    return SyncPlan(
        has_work=bool(matrix),
        releases=releases,
        matrix=matrix,
        rows=rows,
    )


def render_step_summary(
    plan: SyncPlan,
    floor_str: str,
    targets: Sequence[str],
) -> str:
    """Render a Markdown summary table for GitHub Actions job summaries."""
    lines = [
        f"### Upstream RocksDB Sync Discovery (`>= {floor_str}`)",
        "",
    ]
    if not plan.rows:
        lines.append(f"No non-draft, non-prerelease upstream releases found >= `{floor_str}`.")
        return "\n".join(lines) + "\n"

    header = "| Upstream Tag | Release Tag | " + " | ".join(f"`{t}`" for t in targets) + " |"
    align = "| :--- | :--- | " + " | ".join(":---:" for _ in targets) + " |"
    lines.extend([header, align])
    for row in plan.rows:
        cells = " | ".join(row.target_statuses[t] for t in targets)
        lines.append(
            f"| `{row.release.upstream_tag}` | `{row.release.release_tag}` | {cells} |"
        )
    lines.append("")
    if plan.has_work:
        lines.append(
            f"Queued **{len(plan.matrix)}** target build(s) across **{len(plan.releases)}** release(s)."
        )
    else:
        lines.append("All checked releases already have all target archives published.")
    return "\n".join(lines) + "\n"


def _request_json_with_next(
    url: str,
    token: str | None,
) -> tuple[Any, str | None, bool]:
    """Fetch a JSON endpoint with auth fallback and transient-error retries."""
    use_auth = bool(token)
    attempt = 0
    max_attempts = 3

    while True:
        attempt += 1
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rocksdb-prebuilt-sync/1.0",
        }
        if use_auth and token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                link_header = resp.headers.get("Link") or ""
                next_match = _NEXT_LINK_RE.search(link_header)
                next_url = next_match.group(1) if next_match else None
                return payload, next_url, use_auth
        except urllib.error.HTTPError as err:
            if use_auth and err.code in (401, 403):
                # Fall back to unauthenticated API call if token lacks scope/expired.
                use_auth = False
                attempt -= 1
                continue
            if err.code in _TRANSIENT_HTTP_CODES and attempt < max_attempts:
                time.sleep(attempt)
                continue
            body = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub API request failed ({err.code}) for {url}: {body}"
            ) from err
        except urllib.error.URLError as err:
            if attempt < max_attempts:
                time.sleep(attempt)
                continue
            raise RuntimeError(
                f"GitHub API network error for {url}: {err.reason}"
            ) from err


def fetch_releases(
    repo: str,
    token: str | None,
    max_pages: int,
) -> list[dict[str, Any]]:
    """Fetch paginated GitHub releases for `owner/repo`."""
    results: list[dict[str, Any]] = []
    next_url: str | None = (
        f"https://api.github.com/repos/{repo}/releases?per_page=100"
    )
    active_token = token
    pages = 0

    while next_url and pages < max_pages:
        pages += 1
        payload, next_url, kept_auth = _request_json_with_next(
            next_url, active_token
        )
        if not kept_auth:
            active_token = None
        if not isinstance(payload, list):
            raise RuntimeError(
                f"Unexpected GitHub API payload for {repo}: expected list"
            )
        results.extend(payload)
    return results


def _parse_targets(raw: str) -> list[str]:
    targets = [part.strip() for part in raw.split(",") if part.strip()]
    if not targets:
        raise ValueError("--targets must contain at least one target triple")
    return targets


def _write_github_output(path: str, plan: SyncPlan) -> None:
    releases_json = json.dumps(plan.releases, separators=(",", ":"))
    matrix_json = json.dumps(plan.matrix, separators=(",", ":"))
    lines = [
        f"has_work={'true' if plan.has_work else 'false'}",
        f"releases_json={releases_json}",
        f"matrix_json={matrix_json}",
    ]
    with Path(path).open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover upstream meta/rocksdb releases missing prebuilt archives."
    )
    parser.add_argument(
        "--upstream-repo",
        default="meta/rocksdb",
        help="Upstream GitHub repository in owner/name form (default: meta/rocksdb)",
    )
    parser.add_argument(
        "--prebuilt-repo",
        required=True,
        help="Prebuilt GitHub repository in owner/name form (e.g. erwin314/rocksdb-prebuilt)",
    )
    parser.add_argument(
        "--floor",
        default="11.13.2",
        help="Inclusive minimum RocksDB version to sync (default: 11.13.2)",
    )
    parser.add_argument(
        "--targets",
        default=",".join(DEFAULT_TARGETS),
        help="Comma-separated Rust target triples to verify/build",
    )
    parser.add_argument(
        "--max-versions",
        type=int,
        default=3,
        help="Maximum incomplete upstream versions to queue per run (default: 3)",
    )
    parser.add_argument(
        "--github-output",
        help="Path to $GITHUB_OUTPUT file for emitting workflow outputs",
    )
    parser.add_argument(
        "--step-summary",
        help="Path to $GITHUB_STEP_SUMMARY file for appending Markdown status",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        floor = parse_floor_version(args.floor)
        targets = _parse_targets(args.targets)
        token = (
            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        ).strip() or None

        raw_upstream = fetch_releases(args.upstream_repo, token, max_pages=2)
        raw_prebuilt = fetch_releases(args.prebuilt_repo, token, max_pages=20)

        upstream_releases = collect_upstream_releases(raw_upstream, floor)
        uploaded_assets = index_uploaded_assets(raw_prebuilt)
        plan = plan_missing_builds(
            upstream_releases=upstream_releases,
            uploaded_assets_by_tag=uploaded_assets,
            targets=targets,
            max_versions=args.max_versions,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    floor_str = f"{floor[0]}.{floor[1]}.{floor[2]}"
    summary = render_step_summary(plan, floor_str, targets)
    print(summary, end="")
    print(f"has_work={'true' if plan.has_work else 'false'}")
    print(f"releases_json={json.dumps(plan.releases, separators=(',', ':'))}")
    print(f"matrix_json={json.dumps(plan.matrix, separators=(',', ':'))}")

    if args.github_output:
        _write_github_output(args.github_output, plan)
    if args.step_summary:
        with Path(args.step_summary).open("a", encoding="utf-8") as fh:
            fh.write(summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
