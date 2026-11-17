#!/usr/bin/env python3
"""Unit tests for discover_missing_builds.py."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_missing_builds as dmb  # noqa: E402


def _make_release(
    tag_name: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
    assets: list[dict] | None = None,
) -> dict:
    return {
        "tag_name": tag_name,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets or [],
    }


def _make_target_assets(version: str, target: str) -> list[dict]:
    return [
        {
            "name": name,
            "state": "uploaded",
            "size": 1024 * 1024,
        }
        for name in dmb.expected_archive_names(version, target)
    ]


class DiscoverMissingBuildsTest(unittest.TestCase):
    def test_floor_filtering_and_out_of_order_backports(self) -> None:
        raw_upstream = [
            _make_release("v11.14"),
            _make_release("v11.13.2"),
            _make_release("v11.12"),
            _make_release("v11.8.1"),
            _make_release("v8.11.5"),
            _make_release("v12.0.0"),
        ]
        floor = dmb.parse_floor_version("11.13.2")
        releases = dmb.collect_upstream_releases(raw_upstream, floor)

        self.assertEqual(
            [(r.version, r.release_tag, r.upstream_tag) for r in releases],
            [
                ("11.13.2", "v11.13.2", "v11.13.2"),
                ("11.14.0", "v11.14.0", "v11.14"),
                ("12.0.0", "v12.0.0", "v12.0.0"),
            ],
        )

    def test_excludes_draft_prerelease_and_non_semver_tags(self) -> None:
        raw_upstream = [
            _make_release("v11.14.0", draft=True),
            _make_release("v11.14.1", prerelease=True),
            _make_release("v11.14.2-rc1"),
            _make_release("nightly"),
            _make_release("v11.14.2"),
        ]
        floor = dmb.parse_floor_version("11.13.2")
        releases = dmb.collect_upstream_releases(raw_upstream, floor)

        self.assertEqual([r.version for r in releases], ["11.14.2"])

    def test_two_part_normalization_and_deduplication_prefers_three_part(self) -> None:
        raw_upstream = [
            _make_release("v11.14"),
            _make_release("v11.14.0"),
            _make_release("v11.15"),
        ]
        floor = dmb.parse_floor_version("v11.14")
        releases = dmb.collect_upstream_releases(raw_upstream, floor)

        self.assertEqual(
            [(r.version, r.release_tag, r.upstream_tag) for r in releases],
            [
                ("11.14.0", "v11.14.0", "v11.14.0"),
                ("11.15.0", "v11.15.0", "v11.15"),
            ],
        )

    def test_partial_release_backfill_and_invalid_assets(self) -> None:
        upstream = dmb.collect_upstream_releases(
            [_make_release("v11.13.2")],
            dmb.parse_floor_version("11.13.2"),
        )
        prebuilt = [
            _make_release(
                "v11.13.2",
                assets=[
                    *_make_target_assets("11.13.2", "x86_64-unknown-linux-gnu"),
                    *_make_target_assets("11.13.2", "aarch64-apple-darwin"),
                    # Incomplete/corrupt assets for arm64 and windows:
                    {
                        "name": "rocksdb-v11.13.2-aarch64-unknown-linux-gnu-ndebug.tar.gz",
                        "state": "uploaded",
                        "size": 12345,
                    },
                    {
                        "name": "rocksdb-v11.13.2-aarch64-unknown-linux-gnu-checked-status.tar.gz",
                        "state": "starter",
                        "size": 12345,
                    },
                    {
                        "name": "rocksdb-v11.13.2-x86_64-pc-windows-msvc-ndebug.tar.gz",
                        "state": "uploaded",
                        "size": 0,
                    },
                    {
                        "name": "rocksdb-v11.13.2-x86_64-pc-windows-msvc-checked-status.tar.gz",
                        "state": "uploaded",
                        "size": 12345,
                    },
                ],
            ),
            # Draft release assets should be ignored:
            _make_release(
                "v11.13.2",
                draft=True,
                assets=_make_target_assets("11.13.2", "x86_64-pc-windows-msvc"),
            ),
        ]

        plan = dmb.plan_missing_builds(
            upstream_releases=upstream,
            uploaded_assets_by_tag=dmb.index_uploaded_assets(prebuilt),
            targets=dmb.DEFAULT_TARGETS,
            max_versions=3,
        )

        self.assertTrue(plan.has_work)
        self.assertEqual(
            plan.releases,
            [
                {
                    "version": "11.13.2",
                    "release_tag": "v11.13.2",
                    "upstream_tag": "v11.13.2",
                }
            ],
        )
        self.assertEqual(
            [(m["target"], m["os"]) for m in plan.matrix],
            [
                ("aarch64-unknown-linux-gnu", "ubuntu-24.04-arm"),
                ("x86_64-pc-windows-msvc", "windows-2025"),
            ],
        )
        self.assertEqual(
            plan.rows[0].target_statuses,
            {
                "x86_64-unknown-linux-gnu": "present",
                "aarch64-unknown-linux-gnu": "queued",
                "aarch64-apple-darwin": "present",
                "x86_64-pc-windows-msvc": "queued",
            },
        )

    def test_max_versions_skips_complete_releases_without_consuming_budget(self) -> None:
        complete_versions = [f"11.13.{i}" for i in range(2, 7)]  # 5 complete releases
        incomplete_versions = [f"11.13.{i}" for i in range(7, 11)]  # 4 incomplete releases

        raw_upstream = [_make_release(f"v{v}") for v in complete_versions + incomplete_versions]
        raw_prebuilt = []
        for v in complete_versions:
            assets = []
            for target in dmb.DEFAULT_TARGETS:
                assets.extend(_make_target_assets(v, target))
            raw_prebuilt.append(_make_release(f"v{v}", assets=assets))

        upstream = dmb.collect_upstream_releases(
            raw_upstream,
            dmb.parse_floor_version("11.13.2"),
        )
        plan = dmb.plan_missing_builds(
            upstream_releases=upstream,
            uploaded_assets_by_tag=dmb.index_uploaded_assets(raw_prebuilt),
            targets=dmb.DEFAULT_TARGETS,
            max_versions=3,
        )

        self.assertTrue(plan.has_work)
        self.assertEqual(
            [r["version"] for r in plan.releases],
            ["11.13.7", "11.13.8", "11.13.9"],
        )
        self.assertEqual(len(plan.matrix), 3 * 4)
        deferred_rows = [row for row in plan.rows if row.deferred]
        self.assertEqual([row.release.version for row in deferred_rows], ["11.13.10"])
        self.assertTrue(
            all(status == "deferred" for status in deferred_rows[0].target_statuses.values())
        )

    def test_clean_noop_when_all_targets_present(self) -> None:
        raw_upstream = [_make_release("v11.13.2")]
        assets = []
        for target in dmb.DEFAULT_TARGETS:
            assets.extend(_make_target_assets("11.13.2", target))
        raw_prebuilt = [_make_release("v11.13.2", assets=assets)]

        plan = dmb.plan_missing_builds(
            upstream_releases=dmb.collect_upstream_releases(
                raw_upstream, dmb.parse_floor_version("11.13.2")
            ),
            uploaded_assets_by_tag=dmb.index_uploaded_assets(raw_prebuilt),
            targets=dmb.DEFAULT_TARGETS,
            max_versions=3,
        )

        self.assertFalse(plan.has_work)
        self.assertEqual(plan.releases, [])
        self.assertEqual(plan.matrix, [])
        summary = dmb.render_step_summary(plan, "11.13.2", dmb.DEFAULT_TARGETS)
        self.assertIn("All checked releases already have all target archives published.", summary)


if __name__ == "__main__":
    unittest.main()
