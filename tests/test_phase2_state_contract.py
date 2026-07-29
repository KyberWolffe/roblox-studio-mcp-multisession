from __future__ import annotations

import unittest

from .helpers import ALLOW_ALL, FakeStudio, make_service


RAW_MODE_PREDICATE_NAMES = frozenset(
    {
        "is_studio",
        "is_edit",
        "is_running",
        "is_run_mode",
        "is_server",
        "is_client",
        "edit_mode_active",
    }
)


class BrokerRecoveryStateContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry, self.catalog, self.service = make_service()
        self.studio = await FakeStudio.create(
            self.registry,
            "State contract",
            self.catalog.remote_names,
        )

    def assert_unavailable_controller_context(self, state) -> None:
        self.assertEqual("broker", state["source"])
        self.assertFalse(state["connected"])
        self.assertEqual(
            "broker_play_transition",
            state["mode_source"],
        )
        self.assertEqual(
            {
                "role": "edit_controller",
                "datamodel_type": "Edit",
                "request_channel_available": False,
            },
            state["controller_context"],
        )
        self.assertEqual([], state["available_datamodel_types"])
        self.assertEqual(
            RAW_MODE_PREDICATE_NAMES,
            frozenset(state["raw_mode_predicates"]),
        )
        for predicate in state["raw_mode_predicates"].values():
            self.assertEqual({"read_ok": False}, predicate)

    async def read_disconnected_state(self):
        state = await self.service.call_tool(
            ALLOW_ALL,
            "get_studio_state_v2",
            {"studio_id": self.studio.studio_id},
        )
        self.assertTrue(self.studio.transport._queue.empty())
        return state

    async def test_disconnected_no_transition_has_fixed_unavailable_shape(self):
        self.assertTrue(self.studio.disconnect())

        state = await self.read_disconnected_state()

        self.assert_unavailable_controller_context(state)
        self.assertEqual("unknown", state["mode"])
        self.assertEqual(
            {"active": False, "state": "unknown"},
            state["play"],
        )

    async def test_ready_play_bridge_never_advertises_server_or_client(self):
        context = (
            self.studio.studio_id,
            self.studio.client_instance_id,
            self.studio.registration.document_epoch,
            self.studio.generation,
            "state-contract-start",
            0,
            0,
        )
        prepared = self.registry.play_bridges.prepare(
            *context,
            ttl_seconds=180,
        )
        transition = context + (prepared["transition_nonce"],)
        attached = self.registry.play_bridges.attach(
            *transition,
            "state-contract-attach",
            "state-contract-server",
            prepared["bridge_token"],
        )
        ready = self.registry.play_bridges.server_ack(
            *transition,
            "state-contract-server",
            "watchdog_armed",
            "state-contract-watchdog",
            None,
            attached["server_token"],
        )
        self.assertTrue(ready["attached"])
        self.assertTrue(ready["watchdog_armed"])
        self.studio.session.play_bridge_uncertain = prepared[
            "transition_nonce"
        ]
        self.assertTrue(self.studio.disconnect())

        state = await self.read_disconnected_state()

        self.assert_unavailable_controller_context(state)
        self.assertEqual("stopping", state["mode"])
        self.assertTrue(state["play"]["active"])
        self.assertTrue(state["play"]["attached"])
        self.assertTrue(state["play"]["watchdog_armed"])
        self.assertNotIn("Server", state["available_datamodel_types"])
        self.assertNotIn("Client", state["available_datamodel_types"])


if __name__ == "__main__":
    unittest.main()
