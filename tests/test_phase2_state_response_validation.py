from __future__ import annotations

import asyncio
import unittest

from studio_mcp_v2.errors import RemoteToolError

from .helpers import FakeStudio, make_service


RAW_MODE_PREDICATE_NAMES = (
    "is_studio",
    "is_edit",
    "is_running",
    "is_run_mode",
    "is_server",
    "is_client",
    "edit_mode_active",
)


class DurableStateResponseValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry, self.catalog, _service = make_service()
        self.studio = await FakeStudio.create(
            self.registry,
            "State response validation",
            set(self.catalog.remote_names)
            | {"studio_get_state", "get_studio_state"},
        )
        self.run_id = "StateResponseValidationRun0001"
        self.session_tag = "012345abcdef"
        self.place_id = 123
        self.game_id = 456
        self.studio.session.metadata.update(
            {
                "run_id": self.run_id,
                "session_tag": self.session_tag,
                "place_id": self.place_id,
                "game_id": self.game_id,
            }
        )

    def valid_state(self):
        predicates = {
            name: {"read_ok": False}
            for name in RAW_MODE_PREDICATE_NAMES
        }
        predicates.update(
            {
                "is_edit": {"read_ok": True, "value": True},
                "is_running": {"read_ok": True, "value": False},
                "edit_mode_active": {"read_ok": True, "value": True},
            }
        )
        return {
            "adapter": "studio-mcp-v2-durable-plugin",
            "source": "studio_controller",
            "connected": True,
            "studio_id": self.studio.studio_id,
            "client_instance_id": self.studio.client_instance_id,
            "document_epoch": self.studio.registration.document_epoch,
            "generation": self.studio.generation,
            "broker_instance_id": "10000000-0000-4000-8000-000000000001",
            "run_id": self.run_id,
            "session_tag": self.session_tag,
            "name": self.studio.name,
            "place_id": self.place_id,
            "game_id": self.game_id,
            "mode": "edit",
            "is_edit": True,
            "mode_source": "controller_predicates",
            "controller_context": {
                "role": "edit_controller",
                "datamodel_type": "Edit",
                "request_channel_available": True,
            },
            "available_datamodel_types": ["Edit"],
            "raw_mode_predicates": predicates,
            "play": {"active": False, "state": "edit"},
        }

    def valid_transition_state(self, state):
        mode_by_state = {
            "starting": "starting",
            "play": "play",
            "stopping": "stopping",
            "settling": "settling",
            "recovery_required": "unknown",
        }
        candidate = self.valid_state()
        candidate["mode"] = mode_by_state[state]
        candidate["is_edit"] = False
        candidate["mode_source"] = "play_transition"
        candidate["play"] = {
            "active": state in {"play", "stopping"},
            "state": state,
            "accepted": True,
            "server_ready": state in {"play", "stopping"},
            "runner_finished": state == "settling",
            "transition_nonce": (
                "20000000-0000-4000-8000-000000000001"
            ),
        }
        if state == "stopping":
            candidate["play"]["stop_command_id"] = (
                "30000000-0000-4000-8000-000000000001"
            )
        elif state == "recovery_required":
            candidate["play"]["error"] = "bounded monitor error"
        return candidate

    async def invoke_state(self, remote_name="studio_get_state"):
        task = asyncio.create_task(
            self.studio.session.invoke(
                remote_name,
                {},
                1_000,
            )
        )
        request = await self.studio.next_request()
        self.assertEqual(remote_name, request["operation"])
        return task, request

    async def assert_rejected_without_mode_change(self, candidate) -> None:
        original_mode = self.studio.session.mode
        original_confirmed = self.studio.session.last_confirmed_mode
        task, request = await self.invoke_state()

        self.assertTrue(self.studio.respond(request, candidate))
        with self.assertRaisesRegex(
            RemoteToolError,
            "^Targeted Studio returned an invalid state response$",
        ):
            await task

        self.assertEqual(original_mode, self.studio.session.mode)
        self.assertEqual(
            original_confirmed,
            self.studio.session.last_confirmed_mode,
        )
        self.assertNotIn(request["request_id"], self.studio.session.pending)

    async def test_malicious_identity_and_source_fields_fail_closed(self):
        mutations = (
            ("adapter", "other-adapter"),
            ("source", "broker"),
            ("connected", False),
            ("studio_id", "00000000-0000-4000-8000-000000000000"),
            (
                "client_instance_id",
                "00000000-0000-4000-8000-000000000001",
            ),
            ("document_epoch", "other-document"),
            ("generation", self.studio.generation + 1),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                candidate = self.valid_state()
                candidate[field] = replacement
                await self.assert_rejected_without_mode_change(candidate)

    async def test_other_session_identity_cannot_cross_state_boundary(self):
        other = await FakeStudio.create(
            self.registry,
            "Other state response",
            {"studio_get_state"},
        )
        candidate = self.valid_state()
        candidate.update(
            {
                "studio_id": other.studio_id,
                "client_instance_id": other.client_instance_id,
                "document_epoch": other.registration.document_epoch,
                "generation": other.generation,
            }
        )

        await self.assert_rejected_without_mode_change(candidate)

        self.assertEqual("edit", other.session.mode)
        self.assertTrue(other.transport._queue.empty())

    async def test_missing_or_extra_top_level_fields_fail_closed(self):
        missing = self.valid_state()
        del missing["play"]
        await self.assert_rejected_without_mode_change(missing)

        extra = self.valid_state()
        extra["image_base64"] = "not-an-image"
        await self.assert_rejected_without_mode_change(extra)

        unknown_extra = self.valid_state()
        unknown_extra["unexpected"] = True
        await self.assert_rejected_without_mode_change(unknown_extra)

    async def test_malicious_controller_contexts_fail_closed(self):
        contexts = (
            {
                "role": "edit_controller",
                "datamodel_type": "Server",
                "request_channel_available": True,
            },
            {
                "role": "edit_controller",
                "datamodel_type": "Edit",
                "request_channel_available": False,
            },
            {
                "role": "edit_controller",
                "datamodel_type": "Edit",
                "request_channel_available": True,
                "unexpected": True,
            },
        )
        for context in contexts:
            with self.subTest(context=context):
                candidate = self.valid_state()
                candidate["controller_context"] = context
                await self.assert_rejected_without_mode_change(candidate)

        for datamodels in ([], ["Server"], ["Edit", "Client"]):
            with self.subTest(available_datamodel_types=datamodels):
                candidate = self.valid_state()
                candidate["available_datamodel_types"] = datamodels
                await self.assert_rejected_without_mode_change(candidate)

    async def test_malformed_predicates_fail_closed(self):
        candidates = []

        missing = self.valid_state()
        del missing["raw_mode_predicates"]["is_client"]
        candidates.append(missing)

        extra = self.valid_state()
        extra["raw_mode_predicates"]["unknown_probe"] = {
            "read_ok": False
        }
        candidates.append(extra)

        non_boolean_read = self.valid_state()
        non_boolean_read["raw_mode_predicates"]["is_edit"] = {
            "read_ok": "true",
            "value": True,
        }
        candidates.append(non_boolean_read)

        unreadable_with_value = self.valid_state()
        unreadable_with_value["raw_mode_predicates"]["is_edit"] = {
            "read_ok": False,
            "value": False,
        }
        candidates.append(unreadable_with_value)

        readable_without_value = self.valid_state()
        readable_without_value["raw_mode_predicates"]["is_edit"] = {
            "read_ok": True
        }
        candidates.append(readable_without_value)

        readable_non_boolean = self.valid_state()
        readable_non_boolean["raw_mode_predicates"]["is_edit"] = {
            "read_ok": True,
            "value": 1,
        }
        candidates.append(readable_non_boolean)

        for index, candidate in enumerate(candidates):
            with self.subTest(case=index):
                await self.assert_rejected_without_mode_change(candidate)

    async def test_unknown_or_incoherent_mode_fails_closed(self):
        unknown = self.valid_state()
        unknown["mode"] = "run_server"
        unknown["is_edit"] = False
        await self.assert_rejected_without_mode_change(unknown)

        non_string = self.valid_state()
        non_string["mode"] = ["play"]
        non_string["is_edit"] = False
        await self.assert_rejected_without_mode_change(non_string)

        incoherent = self.valid_state()
        incoherent["mode"] = "play"
        incoherent["is_edit"] = True
        incoherent["mode_source"] = "play_transition"
        await self.assert_rejected_without_mode_change(incoherent)

        impossible_predicates = self.valid_state()
        impossible_predicates["raw_mode_predicates"]["is_running"] = {
            "read_ok": True,
            "value": True,
        }
        await self.assert_rejected_without_mode_change(
            impossible_predicates
        )

    async def test_missing_extra_oversize_or_incoherent_play_fails_closed(self):
        extra = self.valid_state()
        extra["play"]["unexpected"] = True

        transition_missing = self.valid_transition_state("play")
        del transition_missing["play"]["accepted"]

        oversize = self.valid_transition_state("recovery_required")
        oversize["play"]["error"] = "x" * 241

        multibyte_oversize = self.valid_transition_state(
            "recovery_required"
        )
        multibyte_oversize["play"]["error"] = "💥" * 61

        wrong_active = self.valid_transition_state("play")
        wrong_active["play"]["active"] = False

        wrong_mode = self.valid_transition_state("play")
        wrong_mode["mode"] = "starting"

        invalid_nonce = self.valid_transition_state("play")
        invalid_nonce["play"]["transition_nonce"] = "x" * 129

        partial_last_state = self.valid_state()
        partial_last_state["play"]["last_state"] = (
            "stopped_edit_confirmed"
        )

        for index, candidate in enumerate(
            (
                extra,
                transition_missing,
                oversize,
                multibyte_oversize,
                wrong_active,
                wrong_mode,
                invalid_nonce,
                partial_last_state,
            )
        ):
            with self.subTest(case=index):
                await self.assert_rejected_without_mode_change(candidate)

    async def test_valid_play_transitions_update_cached_mode(self):
        for state, expected_mode in (
            ("starting", "starting"),
            ("play", "play"),
            ("stopping", "stopping"),
            ("settling", "settling"),
            ("recovery_required", "unknown"),
        ):
            with self.subTest(state=state):
                candidate = self.valid_transition_state(state)
                task, request = await self.invoke_state()

                self.assertTrue(self.studio.respond(request, candidate))

                self.assertEqual(candidate, await task)
                self.assertEqual(expected_mode, self.studio.session.mode)
                self.assertEqual(
                    expected_mode,
                    self.studio.session.last_confirmed_mode,
                )

    async def test_controller_predicate_modes_derive_exactly(self):
        edit = self.valid_state()

        play = self.valid_state()
        play["raw_mode_predicates"]["is_running"] = {
            "read_ok": True,
            "value": True,
        }
        play["mode"] = "play"
        play["is_edit"] = False

        unknown = self.valid_state()
        for name in (
            "is_edit",
            "is_running",
            "edit_mode_active",
        ):
            unknown["raw_mode_predicates"][name] = {
                "read_ok": False
            }
        unknown["mode"] = "unknown"
        unknown["is_edit"] = False

        for label, candidate, expected_mode in (
            ("edit", edit, "edit"),
            ("play", play, "play"),
            ("unknown", unknown, "unknown"),
        ):
            with self.subTest(label=label):
                task, request = await self.invoke_state()
                self.assertTrue(self.studio.respond(request, candidate))
                self.assertEqual(candidate, await task)
                self.assertEqual(expected_mode, self.studio.session.mode)

    async def test_valid_last_play_receipt_is_bounded_and_accepted(self):
        candidate = self.valid_state()
        candidate["play"].update(
            {
                "last_state": "start_failed_edit_confirmed",
                "last_outcome": "start_failed_edit_confirmed",
                "last_transition_nonce": (
                    "40000000-0000-4000-8000-000000000001"
                ),
                "last_failure_code": "request_exception_http_disabled",
            }
        )
        task, request = await self.invoke_state()

        self.assertTrue(self.studio.respond(request, candidate))

        self.assertEqual(candidate, await task)
        self.assertEqual("edit", self.studio.session.mode)

    async def test_legacy_state_alias_is_not_subject_to_durable_contract(self):
        legacy = {"mode": "legacy-mode"}
        task, request = await self.invoke_state("get_studio_state")

        self.assertTrue(self.studio.respond(request, legacy))

        self.assertEqual(legacy, await task)
        self.assertEqual("legacy-mode", self.studio.session.mode)


if __name__ == "__main__":
    unittest.main()
