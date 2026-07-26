"""Review or import a local upstream Studio MCP catalog snapshot.

This command never fetches a URL and never publishes upstream tools into the
durable v2 catalog. New/renamed/schema-changed tools remain review-only. An
atomic local snapshot replacement requires the explicit approval switch and
always writes an exact backup first.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from studio_mcp_v2.catalog_review import (
    DEFAULT_COMPATIBILITY_MANIFEST,
    DEFAULT_DURABLE_CATALOG,
    import_reviewed_catalog,
    installed_v1_cache_candidate,
    load_catalog,
    load_compatibility_manifest,
    prepare_catalog_import,
    rollback_catalog_import,
    review_catalogs,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ROOT / "config" / "tool-catalog.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "candidate",
        type=Path,
        nargs="?",
        help="explicit local upstream catalog artifact",
    )
    parser.add_argument(
        "--installed-v1-cache",
        action="store_true",
        help=(
            "use the exact pwd-resolved per-user v1 "
            "Library/Application Support/StudioMCP/tools-cache.json"
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="operator-owned local upstream snapshot",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically replace the local snapshot after validation",
    )
    parser.add_argument(
        "--prepare-import",
        action="store_true",
        help=(
            "run the full import and generated-catalog contracts without "
            "writing; suitable before a managed broker stop"
        ),
    )
    parser.add_argument(
        "--approve-reviewed-changes",
        action="store_true",
        help="confirm that every reported compatible change was reviewed",
    )
    parser.add_argument(
        "--accept-sha256",
        help=(
            "exact lowercase candidate digest reported by a prior review; "
            "required with --apply"
        ),
    )
    parser.add_argument(
        "--regenerate-durable",
        action="store_true",
        help=(
            "regenerate only mapped exact-handler schemas and provenance; "
            "never expose raw upstream names"
        ),
    )
    parser.add_argument(
        "--compatibility-manifest",
        type=Path,
        default=DEFAULT_COMPATIBILITY_MANIFEST,
    )
    parser.add_argument(
        "--durable-catalog",
        type=Path,
        default=DEFAULT_DURABLE_CATALOG,
    )
    parser.add_argument(
        "--rollback-receipt",
        type=Path,
        help="restore exact catalog backups from one prior import receipt",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.rollback_receipt is not None:
        if (
            args.candidate is not None
            or args.installed_v1_cache
            or args.apply
            or args.prepare_import
            or args.approve_reviewed_changes
            or args.accept_sha256 is not None
            or args.regenerate_durable
        ):
            raise ValueError(
                "--rollback-receipt cannot be combined with review/import"
            )
        print(
            json.dumps(
                rollback_catalog_import(args.rollback_receipt),
                indent=2,
                sort_keys=True,
            )
        )
        return
    if (
        args.candidate is None
        and not args.installed_v1_cache
    ) or (
        args.candidate is not None
        and args.installed_v1_cache
    ):
        raise ValueError(
            "provide exactly one explicit candidate or --installed-v1-cache"
        )
    candidate = (
        args.candidate
        if args.candidate is not None
        else installed_v1_cache_candidate()
    )
    if args.approve_reviewed_changes and not args.apply:
        raise ValueError("--approve-reviewed-changes requires --apply")
    if args.apply and args.accept_sha256 is None:
        raise ValueError("--apply requires --accept-sha256 from a prior review")
    if args.accept_sha256 is not None and not args.apply:
        raise ValueError("--accept-sha256 requires --apply")
    if args.apply and args.prepare_import:
        raise ValueError("--apply and --prepare-import are mutually exclusive")
    if args.regenerate_durable and not (
        args.apply or args.prepare_import
    ):
        raise ValueError(
            "--regenerate-durable requires --apply or --prepare-import"
        )
    if args.prepare_import:
        result = prepare_catalog_import(
            args.baseline,
            candidate,
            compatibility_manifest_path=args.compatibility_manifest,
            durable_catalog_path=args.durable_catalog,
            regenerate_durable=args.regenerate_durable,
        )
    elif args.apply:
        result = import_reviewed_catalog(
            args.baseline,
            candidate,
            approve_reviewed_changes=args.approve_reviewed_changes,
            expected_candidate_sha256=args.accept_sha256,
            compatibility_manifest_path=args.compatibility_manifest,
            durable_catalog_path=args.durable_catalog,
            regenerate_durable=args.regenerate_durable,
        )
    else:
        baseline, baseline_bytes = load_catalog(args.baseline)
        candidate_payload, candidate_bytes = load_catalog(candidate)
        durable_payload, _durable_bytes = load_catalog(
            args.durable_catalog
        )
        result = review_catalogs(
            baseline,
            candidate_payload,
            baseline_bytes=baseline_bytes,
            candidate_bytes=candidate_bytes,
            compatibility_manifest=load_compatibility_manifest(
                args.compatibility_manifest
            ),
            durable_payload=durable_payload,
        ).as_dict()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
