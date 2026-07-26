#!/usr/bin/env python3
"""Validate the exact modern-v1 to safe-v2 capability parity contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from release_tools.installer import VERSION  # noqa: E402
from studio_mcp_v2.catalog import JOB_TOOLS  # noqa: E402


PARITY_PATH = Path("config/v1-capability-parity.json")
UPSTREAM_PATH = Path("config/tool-catalog.json")
DURABLE_PATH = Path("config/durable-tool-catalog.json")
PARITY_DOC_PATH = Path("docs/CAPABILITY_PARITY.md")

LEGACY_ALIASES = (
    "GetConsoleOutput",
    "GetStudioMode",
    "InsertModel",
    "RunCode",
    "RunScriptInPlayMode",
    "StartStopPlay",
)
ALLOWED_STATUSES = (
    "v2_full",
    "v2_partial",
    "native_codex_equivalent",
    "deferred",
)
COMPLETE_STATUSES = frozenset({"v2_full", "native_codex_equivalent"})
ALLOWED_PRIORITIES = frozenset({"P0", "P1", "P2"})
NATIVE_CODEX_CAPABILITIES = frozenset(
    {"web_browsing", "multi_agent_delegation", "codex_skills"}
)
FRIENDLY_STATUS = {
    "v2_full": "v2 full",
    "v2_partial": "v2 partial",
    "native_codex_equivalent": "native Codex equivalent",
    "deferred": "deferred",
}
NATIVE_REFERENCE_LABELS = {
    "web_browsing": "Codex web browsing",
    "multi_agent_delegation": "Codex multi-agent delegation",
    "codex_skills": "Codex skills",
}
INCOMPLETE_RELEASE_MARKERS = (
    "<!-- experimental-prerelease: true -->",
    "<!-- capability-parity: incomplete -->",
    "<!-- global-v1-fallback: forbidden -->",
)
FORBIDDEN_POSITIVE_CLAIMS = (
    "full capability parity achieved",
    "full tool parity achieved",
    "complete capability parity achieved",
    "all 25 modern tools are fully supported",
    "100% capability parity",
)
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class ParityValidationError(ValueError):
    """The parity ledger, references, version, or documentation drifted."""


def _load_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParityValidationError(label + " is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ParityValidationError(label + " must contain an object")
    return value


def _tool_names(payload: Mapping[str, Any], label: str) -> Sequence[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise ParityValidationError(label + " tools must be an array")
    names = []
    for item in tools:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ParityValidationError(label + " has an invalid tool entry")
        names.append(item["name"])
    if len(names) != len(set(names)):
        raise ParityValidationError(label + " has duplicate tool names")
    return names


def validate_version_policy(
    version: str,
    *,
    p0_incomplete: bool,
    expected_tag: Optional[str] = None,
) -> None:
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ParityValidationError("release version is not semantic")
    if p0_incomplete and match.group(4) is None:
        raise ParityValidationError(
            "P0 parity is incomplete, so the release version must be a prerelease"
        )
    if expected_tag is not None and expected_tag != "v" + version:
        raise ParityValidationError(
            "release tag must exactly equal v" + version
        )
    if p0_incomplete and expected_tag is not None and "-" not in expected_tag:
        raise ParityValidationError(
            "P0 parity is incomplete, so the release tag must be a prerelease"
        )


def _validate_reference(
    reference: Any,
    *,
    durable_names: frozenset,
    broker_names: frozenset,
) -> str:
    if not isinstance(reference, Mapping) or set(reference) != {"kind", "name"}:
        raise ParityValidationError(
            "each capability reference must contain exactly kind and name"
        )
    kind = reference.get("kind")
    name = reference.get("name")
    if not isinstance(kind, str) or not isinstance(name, str) or not name:
        raise ParityValidationError("capability reference fields are invalid")
    if kind == "durable_tool":
        if name not in durable_names:
            raise ParityValidationError(
                "parity ledger references unknown durable tool: " + name
            )
    elif kind == "broker_tool":
        if name not in broker_names:
            raise ParityValidationError(
                "parity ledger references unknown broker tool: " + name
            )
    elif kind == "native_codex":
        if name not in NATIVE_CODEX_CAPABILITIES:
            raise ParityValidationError(
                "parity ledger references unknown native Codex capability: "
                + name
            )
    else:
        raise ParityValidationError("unknown capability reference kind: " + kind)
    return kind


def _public_markdown(root: Path) -> Iterable[Path]:
    for path in (root / "README.md", root / "SECURITY.md"):
        if path.is_file():
            yield path
    docs = root / "docs"
    if docs.is_dir():
        yield from sorted(docs.glob("*.md"))


def _reference_label(reference: Mapping[str, str]) -> str:
    kind = reference["kind"]
    name = reference["name"]
    if kind == "durable_tool":
        return "`" + name + "_v2`"
    if kind == "broker_tool":
        return "`" + name + "`"
    return NATIVE_REFERENCE_LABELS[name]


def validate_capability_parity(
    project_root: Path = PROJECT_ROOT,
    *,
    release_version: Optional[str] = None,
    expected_tag: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(project_root).resolve(strict=True)
    parity = _load_object(root / PARITY_PATH, "capability parity ledger")
    upstream = _load_object(root / UPSTREAM_PATH, "upstream tool catalog")
    durable = _load_object(root / DURABLE_PATH, "durable tool catalog")

    if set(parity) != {
        "schema_version",
        "release_version",
        "baseline",
        "policy",
        "tools",
    } or parity.get("schema_version") != 1:
        raise ParityValidationError("capability parity top-level schema drifted")

    upstream_names = list(_tool_names(upstream, "upstream tool catalog"))
    if len(upstream_names) != 31:
        raise ParityValidationError(
            "baseline must contain exactly 25 modern tools and 6 legacy aliases"
        )
    if tuple(upstream_names[-6:]) != LEGACY_ALIASES:
        raise ParityValidationError("the six excluded legacy aliases drifted")
    modern_names = upstream_names[:25]

    baseline = parity.get("baseline")
    if not isinstance(baseline, Mapping) or baseline != {
        "catalog_path": UPSTREAM_PATH.as_posix(),
        "modern_tool_count": 25,
        "legacy_alias_count": 6,
        "excluded_legacy_aliases": list(LEGACY_ALIASES),
    }:
        raise ParityValidationError("capability parity baseline metadata drifted")

    policy = parity.get("policy")
    if not isinstance(policy, Mapping):
        raise ParityValidationError("capability parity policy is invalid")
    if policy.get("allowed_statuses") != list(ALLOWED_STATUSES):
        raise ParityValidationError("allowed parity statuses drifted")
    if policy.get("complete_statuses") != [
        "v2_full",
        "native_codex_equivalent",
    ]:
        raise ParityValidationError("complete parity statuses drifted")
    if policy.get("no_global_v1_fallback") is not True:
        raise ParityValidationError("global v1 fallback must remain forbidden")
    if policy.get("full_parity_claimed") is not False:
        raise ParityValidationError("this incomplete release cannot claim full parity")

    tools = parity.get("tools")
    if not isinstance(tools, list):
        raise ParityValidationError("capability parity tools must be an array")
    matrix_names = [
        item.get("name") if isinstance(item, Mapping) else None for item in tools
    ]
    if matrix_names != modern_names:
        raise ParityValidationError(
            "capability parity must cover the exact ordered 25-tool modern set"
        )

    durable_names = frozenset(_tool_names(durable, "durable tool catalog"))
    broker_names = frozenset(
        item["name"]
        for item in JOB_TOOLS
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    )
    p0_gap_names = []
    status_counts = {status: 0 for status in ALLOWED_STATUSES}
    document_rows = []
    required_entry_keys = {
        "name",
        "priority",
        "status",
        "p0_gap",
        "references",
        "gap",
        "phase",
    }
    for item in tools:
        if not isinstance(item, Mapping) or set(item) != required_entry_keys:
            raise ParityValidationError("capability parity tool schema drifted")
        name = item["name"]
        priority = item["priority"]
        status = item["status"]
        if priority not in ALLOWED_PRIORITIES:
            raise ParityValidationError(name + " has an invalid priority")
        if status not in ALLOWED_STATUSES:
            raise ParityValidationError(name + " has an invalid status")
        status_counts[status] += 1

        references = item["references"]
        if not isinstance(references, list):
            raise ParityValidationError(name + " references must be an array")
        kinds = [
            _validate_reference(
                reference,
                durable_names=durable_names,
                broker_names=broker_names,
            )
            for reference in references
        ]
        gap = item["gap"]
        if status in {"v2_full", "v2_partial"}:
            if not references or any(kind == "native_codex" for kind in kinds):
                raise ParityValidationError(
                    name + " must reference at least one real v2 tool"
                )
        elif status == "native_codex_equivalent":
            if not references or any(kind != "native_codex" for kind in kinds):
                raise ParityValidationError(
                    name + " must reference a native Codex capability"
                )
        elif references:
            raise ParityValidationError(name + " is deferred but has a route")

        if status in {"v2_partial", "deferred"}:
            if not isinstance(gap, str) or not gap.strip():
                raise ParityValidationError(name + " must state its parity gap")
            expected_phase = "phase_2"
        else:
            if gap is not None:
                raise ParityValidationError(name + " is complete but states a gap")
            expected_phase = "phase_1_complete"
        if item["phase"] != expected_phase:
            raise ParityValidationError(name + " has an inconsistent phase")

        expected_p0_gap = priority == "P0" and status not in COMPLETE_STATUSES
        if item["p0_gap"] is not expected_p0_gap:
            raise ParityValidationError(name + " has an inconsistent P0 gap flag")
        if expected_p0_gap:
            p0_gap_names.append(name)
        if isinstance(gap, str) and "|" in gap:
            raise ParityValidationError(name + " gap cannot contain a table delimiter")
        route = (
            ", ".join(_reference_label(reference) for reference in references)
            if references
            else "None"
        )
        document_rows.append(
            "| `"
            + name
            + "` | "
            + priority
            + " | "
            + FRIENDLY_STATUS[status]
            + " | "
            + ("**Yes**" if expected_p0_gap else "No")
            + " | "
            + route
            + " | "
            + (gap if isinstance(gap, str) else "None")
            + " |"
        )

    p0_incomplete = bool(p0_gap_names)
    if policy.get("p0_complete") is not (not p0_incomplete):
        raise ParityValidationError("p0_complete does not match the tool ledger")

    actual_version = VERSION if release_version is None else release_version
    if parity.get("release_version") != actual_version:
        raise ParityValidationError(
            "capability parity release_version does not match the package version"
        )
    validate_version_policy(
        actual_version,
        p0_incomplete=p0_incomplete,
        expected_tag=expected_tag,
    )

    parity_doc = root / PARITY_DOC_PATH
    try:
        document = parity_doc.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ParityValidationError("capability parity documentation is missing") from exc
    required_markers = {
        "<!-- parity-matrix: config/v1-capability-parity.json -->",
        "<!-- full-parity-claimed: false -->",
        "<!-- p0-parity: incomplete -->"
        if p0_incomplete
        else "<!-- p0-parity: complete -->",
    }
    for marker in required_markers:
        if marker not in document:
            raise ParityValidationError("capability parity marker is missing: " + marker)
    table_start_marker = "## Exact 25-tool matrix\n\n"
    table_end_marker = "\n\n## Publication gate"
    if (
        document.count(table_start_marker) != 1
        or document.count(table_end_marker) != 1
    ):
        raise ParityValidationError(
            "capability parity documentation table boundaries drifted"
        )
    table_start = document.index(table_start_marker) + len(table_start_marker)
    table_end = document.index(table_end_marker, table_start)
    actual_table = document[table_start:table_end].splitlines()
    expected_table = [
        (
            "| Tool | Priority | Status | P0 gap | Current route | "
            "Phase 2 requirement |"
        ),
        "|---|---:|---|:---:|---|---|",
        *document_rows,
    ]
    if actual_table != expected_table:
        raise ParityValidationError(
            "capability parity documentation row drifted"
        )

    if p0_incomplete:
        release_notes = (
            root / "docs" / ("RELEASE_NOTES_" + actual_version + ".md")
        )
        publication_files = (root / "README.md", release_notes)
        for path in publication_files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ParityValidationError(
                    "prerelease disclosure document is missing: "
                    + str(path.relative_to(root))
                ) from exc
            for marker in INCOMPLETE_RELEASE_MARKERS:
                if text.count(marker) != 1:
                    raise ParityValidationError(
                        "prerelease disclosure marker drifted in "
                        + str(path.relative_to(root))
                        + ": "
                        + marker
                    )
        for path in _public_markdown(root):
            text = path.read_text(encoding="utf-8").lower()
            for claim in FORBIDDEN_POSITIVE_CLAIMS:
                if claim in text:
                    raise ParityValidationError(
                        "incomplete prerelease documentation makes a positive "
                        "full-parity claim in "
                        + str(path.relative_to(root))
                    )

    return {
        "ok": True,
        "release_version": actual_version,
        "expected_tag": expected_tag,
        "modern_tool_count": len(modern_names),
        "excluded_legacy_alias_count": len(LEGACY_ALIASES),
        "status_counts": status_counts,
        "p0_complete": not p0_incomplete,
        "p0_gap_count": len(p0_gap_names),
        "p0_gaps": p0_gap_names,
        "no_global_v1_fallback": True,
        "full_parity_claimed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the exact 25-tool capability ledger, references, "
            "documentation, and prerelease publication gate."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
    )
    parser.add_argument(
        "--tag",
        help="optional exact release tag; must match the package version",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = validate_capability_parity(
            args.project_root,
            expected_tag=args.tag,
        )
    except (OSError, ParityValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
