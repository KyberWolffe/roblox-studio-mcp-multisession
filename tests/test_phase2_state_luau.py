from __future__ import annotations

import unittest
from pathlib import Path

from scripts import render_studio_plugin


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "studio_plugin_template.luau"
HANDLERS = ROOT / "scripts" / "durable_operation_handlers.luau"
TOKEN = "t" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"


class Phase2StateLuauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.handlers = HANDLERS.read_text(encoding="utf-8")

    def test_mode_snapshot_preserves_each_probe_read_result(self) -> None:
        probe = self.template[
            self.template.index("local function booleanModeProbe("):
            self.template.index("local function requireEditMode(")
        ]
        self.assertIn("read_ok = true", probe)
        self.assertIn("value = value", probe)
        self.assertIn("read_ok = false", probe)
        self.assertIn("raw_mode_predicates = rawPredicates", probe)

        for method in (
            "RunService:IsStudio()",
            "RunService:IsEdit()",
            "RunService:IsRunning()",
            "RunService:IsRunMode()",
            "RunService:IsServer()",
            "RunService:IsClient()",
            "StudioTestService.EditModeActive",
        ):
            self.assertEqual(1, probe.count(method), method)

    def test_observed_mode_reports_its_evidence_source(self) -> None:
        observed = self.template[
            self.template.index(
                "local function observedMode(play, controllerMode)"
            ):
            self.template.index("local function monitorPlayTransition(")
        ]
        for mode in ("starting", "play", "stopping", "settling"):
            self.assertIn(
                f'return "{mode}", "play_transition"',
                observed,
            )
        self.assertIn(
            'return "unknown", "play_transition"',
            observed,
        )
        self.assertIn(
            'return sampledMode.mode, "controller_predicates"',
            observed,
        )

    def test_durable_state_uses_one_sample_and_only_edit_route(self) -> None:
        state = self.handlers[
            self.handlers.index("local function durableGetState()"):
            self.handlers.index("local TREE_CURSOR_KEYS")
        ]
        self.assertEqual(1, state.count("modeSnapshot()"))
        self.assertIn(
            "local mode, modeSource = observedMode(play, controllerMode)",
            state,
        )
        self.assertIn('source = "studio_controller"', state)
        self.assertIn("connected = true", state)
        self.assertIn("mode_source = modeSource", state)
        self.assertIn(
            "raw_mode_predicates = controllerMode.raw_mode_predicates",
            state,
        )
        self.assertIn('role = "edit_controller"', state)
        self.assertIn('datamodel_type = "Edit"', state)
        self.assertIn("request_channel_available = true", state)
        self.assertIn('available_datamodel_types = { "Edit" }', state)
        self.assertNotIn('datamodel_type = "Server"', state)
        self.assertNotIn('datamodel_type = "Client"', state)

    def test_renderer_composes_the_connected_state_contract(self) -> None:
        rendered = render_studio_plugin.render_durable(TOKEN, RUN_ID)
        self.assertIn("local function booleanModeProbe(", rendered)
        self.assertIn(
            "local mode, modeSource = observedMode(play, controllerMode)",
            rendered,
        )
        self.assertIn(
            'available_datamodel_types = { "Edit" }',
            rendered,
        )
        self.assertNotIn(
            'available_datamodel_types = { "Server"',
            rendered,
        )
        self.assertNotIn(
            'available_datamodel_types = { "Client"',
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
