from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable

from .helpers import ALLOW_ALL, FakeStudio, make_service


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "synthetic"
    / "named-studio-sessions.json"
)


def _resolve_named_session(
    discovery: Iterable[Dict[str, Any]],
    place_name: str,
) -> str:
    """Model the documented Codex workflow, not a broker-side default."""

    matches = [
        item
        for item in discovery
        if item.get("connected") is True
        and item.get("metadata", {}).get("name") == place_name
    ]
    if len(matches) != 1:
        raise ValueError("named Studio session is absent or ambiguous")
    return str(matches[0]["studio_id"])


class NamedSessionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tasks_resolve_names_then_route_internal_ids_in_parallel(
        self,
    ) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual("synthetic", fixture["release_audit_fixture"])
        # The user-facing task payloads contain ordinary names, never UUIDs.
        for request in fixture["user_requests"]:
            self.assertEqual({"place_name", "task"}, set(request))
            self.assertNotIn("studio_id", request)

        registry, catalog, service = make_service()
        studios = []
        by_name = {}
        for configured in fixture["sessions"]:
            metadata = configured["metadata"]
            studio = await FakeStudio.create(
                registry,
                metadata["name"],
                catalog.remote_names,
            )
            studio.session.metadata.update(metadata)
            studios.append(studio)
            by_name[studio.name] = studio

        discovered = service.list_studios(ALLOW_ALL)["studios"]
        internal_targets = {
            request["place_name"]: _resolve_named_session(
                discovered, request["place_name"]
            )
            for request in fixture["user_requests"]
        }
        self.assertEqual(2, len(set(internal_targets.values())))

        calls = {
            name: asyncio.create_task(
                service.call_tool(
                    ALLOW_ALL,
                    "get_console_output_v2",
                    {"studio_id": studio_id},
                )
            )
            for name, studio_id in internal_targets.items()
        }
        requests = await asyncio.gather(
            *(by_name[name].next_request() for name in calls)
        )
        self.assertTrue(all(not call.done() for call in calls.values()))
        for name, request in zip(calls, requests):
            self.assertEqual(internal_targets[name], request["studio_id"])
            self.assertEqual(
                internal_targets[name], by_name[name].studio_id
            )
            by_name[name].respond(request, {"place": name})
        results = await asyncio.gather(*calls.values())
        self.assertEqual(
            {request["place_name"] for request in fixture["user_requests"]},
            {result["place"] for result in results},
        )

    async def test_name_resolution_refuses_ambiguity_instead_of_guessing(
        self,
    ) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        duplicate_name = fixture["sessions"][0]["metadata"]["name"]
        discovery = [
            {
                "connected": True,
                "studio_id": item["studio_id"],
                "metadata": {"name": duplicate_name},
            }
            for item in fixture["sessions"]
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            _resolve_named_session(discovery, duplicate_name)


if __name__ == "__main__":
    unittest.main()
