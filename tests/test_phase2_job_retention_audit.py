from __future__ import annotations

import copy
import unittest

from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.errors import SessionConflictError
from studio_mcp_v2.multi_edit import canonical_json_sha256
from studio_mcp_v2.registry import SessionRegistry
from studio_mcp_v2.session import (
    MAX_ACTIVE_SESSION_JOBS,
    MAX_JOB_RETIREMENT_TOMBSTONES,
    MAX_SESSION_JOBS,
    JobRecord,
)

from .helpers import PROJECT_ROOT, FakeStudio


DURABLE_CATALOG = (
    PROJECT_ROOT / "config" / "durable-tool-catalog.json"
)


class Phase2JobRetentionAuditTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(DURABLE_CATALOG)
        self.studio = await FakeStudio.create(
            self.registry,
            "Retention audit",
            self.catalog.remote_names,
        )

    def record(self, index: int, status: str) -> JobRecord:
        result = (
            {"index": index, "secret_result": "must-not-tombstone"}
            if status == "completed"
            else None
        )
        return JobRecord(
            job_id="job-" + str(index),
            studio_id=self.studio.studio_id,
            generation=self.studio.generation,
            public_tool="studio_search_scripts_v2",
            remote_tool="studio_search_scripts",
            arguments={
                "secret_argument": "must-not-tombstone",
                "index": index,
            },
            timeout_ms=1_000,
            status=status,
            result=result,
            client_instance_id=self.studio.client_instance_id,
            document_epoch=self.studio.registration.document_epoch,
            input_schema_sha256="1" * 64,
            output_schema_sha256="2" * 64,
            handler_contract_sha256="3" * 64,
            arguments_sha256=canonical_json_sha256(
                {
                    "secret_argument": "must-not-tombstone",
                    "index": index,
                }
            ),
            terminal_outcome=(
                "completed" if status == "completed" else None
            ),
        )

    async def test_terminal_compaction_keeps_a_bounded_recomputable_source_free_chain(
        self,
    ) -> None:
        for index in range(200):
            record = self.record(index, "completed")
            record.created_at = float(index)
            record.updated_at = float(index)
            self.studio.session.jobs[record.job_id] = record

        self.studio.session._compact_terminal_jobs()

        self.assertEqual(
            MAX_SESSION_JOBS - 1,
            len(self.studio.session.jobs),
        )
        self.assertEqual(
            200 - (MAX_SESSION_JOBS - 1),
            self.studio.session.job_retirement_count,
        )
        tombstones = list(
            self.studio.session.job_retirement_tombstones
        )
        self.assertEqual(
            MAX_JOB_RETIREMENT_TOMBSTONES,
            len(tombstones),
        )
        previous = tombstones[0]["previous_chain_sha256"]
        for tombstone in tombstones:
            self.assertEqual(
                previous, tombstone["previous_chain_sha256"]
            )
            retired = copy.deepcopy(tombstone)
            expected_chain = retired.pop("chain_sha256")
            retired.pop("previous_chain_sha256")
            self.assertEqual(
                expected_chain,
                canonical_json_sha256(
                    {
                        "previous_sha256": previous,
                        "retired": retired,
                    }
                ),
            )
            self.assertNotIn("arguments", tombstone)
            self.assertNotIn("result", tombstone)
            self.assertNotIn(
                "secret_argument", repr(tombstone)
            )
            self.assertNotIn(
                "secret_result", repr(tombstone)
            )
            previous = expected_chain
        self.assertEqual(
            previous,
            self.studio.session.job_retirement_chain_sha256,
        )

    async def test_active_and_uncertain_records_are_never_compacted(
        self,
    ) -> None:
        statuses = ("queued", "running", "outcome_unknown")
        for index in range(150):
            record = self.record(index, statuses[index % 3])
            self.studio.session.jobs[record.job_id] = record

        self.studio.session._compact_terminal_jobs()

        self.assertEqual(150, len(self.studio.session.jobs))
        self.assertEqual(
            0, self.studio.session.job_retirement_count
        )
        self.assertEqual(
            [], list(self.studio.session.job_retirement_tombstones)
        )

    async def test_active_job_limit_rejects_without_retiring_or_dispatching(
        self,
    ) -> None:
        for index in range(MAX_ACTIVE_SESSION_JOBS):
            record = self.record(index, "queued")
            self.studio.session.jobs[record.job_id] = record

        with self.assertRaises(SessionConflictError):
            self.studio.session.start_job(
                "studio_search_scripts_v2",
                "studio_search_scripts",
                {
                    "keywords": "Player",
                    "root_path": ["Workspace"],
                    "max_depth": 3,
                    "scan_limit": 10,
                    "page_size": 2,
                    "time_limit_ms": 1_000,
                },
                1_000,
            )
        self.assertEqual(
            MAX_ACTIVE_SESSION_JOBS,
            len(self.studio.session.jobs),
        )
        self.assertTrue(self.studio.transport._queue.empty())
        self.assertEqual(
            0, self.studio.session.job_retirement_count
        )


if __name__ == "__main__":
    unittest.main()
