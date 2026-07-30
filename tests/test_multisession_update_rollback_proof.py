from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from release_tools import builder
from release_tools.multisession_update_proof import (
    _CANDIDATE_SOURCE_MAP,
    CANDIDATE_VERSION,
    CANONICAL_SERVER_HEADER,
    PRIOR_ARCHIVE_SHA256,
    PRIOR_BOOTSTRAP_SHA256,
    PRIOR_INSTALLER_SHA256,
    PRIOR_MANIFEST_SHA256,
    PRIOR_SOURCE_COMMIT,
    PRIOR_SOURCE_TREE,
    PRIOR_UPDATER_SHA256,
    PRIOR_VERSION,
    ProofError,
    _assert_transaction,
    _candidate_source_binding,
    _fingerprint_sha256,
    _registration_evidence,
    prove_multisession_update_rollback,
)
from scripts.prove_multisession_update_rollback import (
    _atomic_write,
    _validated_output_paths,
)


class MultisessionUpdateRollbackProofTests(unittest.TestCase):
    @staticmethod
    def _source_manifest():
        blobs = {
            source_path: ("source:" + source_path).encode("utf-8")
            for source_path, _archive_path, _mode
            in _CANDIDATE_SOURCE_MAP
        }
        manifest = {
            "files": [
                {
                    "path": archive_path,
                    "sha256": hashlib.sha256(
                        blobs[source_path]
                    ).hexdigest(),
                    "size": len(blobs[source_path]),
                    "mode": mode,
                }
                for source_path, archive_path, mode
                in _CANDIDATE_SOURCE_MAP
            ]
        }
        return manifest, blobs

    def test_exact_published_rc5_identity_is_pinned(self) -> None:
        self.assertEqual("0.4.0-rc.5", PRIOR_VERSION)
        self.assertEqual("0.4.0-rc.6", CANDIDATE_VERSION)
        self.assertEqual(
            (
                "d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8b"
                "e2d7f949012b559d"
            ),
            PRIOR_ARCHIVE_SHA256,
        )
        self.assertEqual(
            (
                "3deb48919dc549c2695dd14621579a6f02ac05b301b697865"
                "aba1393a53372ef"
            ),
            PRIOR_MANIFEST_SHA256,
        )
        self.assertEqual(
            (
                "93ed2e076e92faa7863ddb975b41b1f6954890d0eb6180f8"
                "68647c35d1ac28b0"
            ),
            PRIOR_INSTALLER_SHA256,
        )
        self.assertEqual(
            (
                "ecc8ec2db2ffda1f4d1c64ddc35db7b8f2735878bdbfa52"
                "de2df0bc4aa756fbe"
            ),
            PRIOR_UPDATER_SHA256,
        )
        self.assertEqual(
            (
                "e4f35d878024a3c73d6276bc512236e1cad8637c98894da9"
                "76b233d556cd346b"
            ),
            PRIOR_BOOTSTRAP_SHA256,
        )
        self.assertEqual(
            "923422254e95050f0fe66bacc0114e9ace2789c5",
            PRIOR_SOURCE_COMMIT,
        )
        self.assertEqual(
            "3e3713045821412b6a6bbe0a4db9e27ab7bb58e3",
            PRIOR_SOURCE_TREE,
        )

    def test_wrong_candidate_version_fails_before_archive_io(
        self,
    ) -> None:
        missing = Path("/does-not-exist")
        with mock.patch(
            "release_tools.multisession_update_proof._archive_manifest",
            side_effect=AssertionError(
                "version mismatch must fail before archive I/O"
            ),
        ):
            with self.assertRaisesRegex(
                ProofError, "requires candidate 0.4.0-rc.6"
            ):
                prove_multisession_update_rollback(
                    prior_archive=missing,
                    prior_checksum_file=missing,
                    candidate_archive=missing,
                    candidate_checksum_file=missing,
                    candidate_expected_sha256="0" * 64,
                    candidate_version="0.4.0-rc.7",
                    source_commit="0" * 40,
                    source_tree="0" * 40,
                )

    def test_invalid_source_identity_fails_before_archive_io(
        self,
    ) -> None:
        missing = Path("/does-not-exist")
        with mock.patch(
            "release_tools.multisession_update_proof._archive_manifest",
            side_effect=AssertionError(
                "source mismatch must fail before archive I/O"
            ),
        ):
            with self.assertRaisesRegex(
                ProofError, "candidate source commit"
            ):
                prove_multisession_update_rollback(
                    prior_archive=missing,
                    prior_checksum_file=missing,
                    candidate_archive=missing,
                    candidate_checksum_file=missing,
                    candidate_expected_sha256="0" * 64,
                    candidate_version=CANDIDATE_VERSION,
                    source_commit="not-a-commit",
                    source_tree="0" * 40,
                )

    def test_source_map_matches_the_closed_portable_builder(self) -> None:
        builder_map = set(builder.PACKAGE_SOURCES)
        builder_map.update(
            {
                (
                    "studio_mcp_v2/" + module,
                    "payload/studio_mcp_v2/" + module,
                    0o644,
                )
                for module in builder.RUNTIME_MODULES
            }
        )
        self.assertEqual(builder_map, set(_CANDIDATE_SOURCE_MAP))

    def test_candidate_archive_manifest_binds_every_source_blob(
        self,
    ) -> None:
        commit = "a" * 40
        tree = "b" * 40
        manifest, blobs = self._source_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def git_output(_root, arguments, **_kwargs):
                if arguments == ("rev-parse", "--show-toplevel"):
                    return (str(root) + "\n").encode("utf-8")
                if arguments == ("cat-file", "-t", commit):
                    return b"commit\n"
                if arguments == ("cat-file", "-t", tree):
                    return b"tree\n"
                if arguments == (
                    "rev-parse",
                    commit + "^{tree}",
                ):
                    return (tree + "\n").encode("ascii")
                if arguments[:2] == ("cat-file", "blob"):
                    object_path = arguments[2]
                    prefix = tree + ":"
                    self.assertTrue(object_path.startswith(prefix))
                    return blobs[object_path[len(prefix) :]]
                raise AssertionError(arguments)

            with mock.patch(
                "release_tools.multisession_update_proof._git_output",
                side_effect=git_output,
            ):
                binding = _candidate_source_binding(
                    manifest,
                    repository=root,
                    source_commit=commit,
                    source_tree=tree,
                )
        self.assertTrue(binding["verified"])
        self.assertEqual(
            len(_CANDIDATE_SOURCE_MAP),
            binding["packaged_source_file_count"],
        )
        self.assertEqual(64, len(binding["source_map_sha256"]))

    def test_source_binding_rejects_noncommit_or_unrelated_tree(
        self,
    ) -> None:
        commit = "a" * 40
        tree = "b" * 40
        manifest, _blobs = self._source_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def identity_output(commit_type, observed_tree):
                def git_output(_root, arguments, **_kwargs):
                    if arguments == ("rev-parse", "--show-toplevel"):
                        return (str(root) + "\n").encode("utf-8")
                    if arguments == ("cat-file", "-t", commit):
                        return commit_type
                    if arguments == ("cat-file", "-t", tree):
                        return b"tree\n"
                    if arguments == (
                        "rev-parse",
                        commit + "^{tree}",
                    ):
                        return observed_tree
                    raise AssertionError(arguments)

                return git_output

            with mock.patch(
                "release_tools.multisession_update_proof._git_output",
                side_effect=identity_output(b"tree\n", b""),
            ):
                with self.assertRaisesRegex(
                    ProofError, "not a commit"
                ):
                    _candidate_source_binding(
                        manifest,
                        repository=root,
                        source_commit=commit,
                        source_tree=tree,
                    )
            with mock.patch(
                "release_tools.multisession_update_proof._git_output",
                side_effect=identity_output(
                    b"commit\n", ("c" * 40 + "\n").encode("ascii")
                ),
            ):
                with self.assertRaisesRegex(
                    ProofError, "not the exact commit tree"
                ):
                    _candidate_source_binding(
                        manifest,
                        repository=root,
                        source_commit=commit,
                        source_tree=tree,
                    )

    def test_source_binding_rejects_manifest_blob_drift(self) -> None:
        commit = "a" * 40
        tree = "b" * 40
        manifest, blobs = self._source_manifest()
        manifest["files"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()

            def git_output(_root, arguments, **_kwargs):
                if arguments == ("rev-parse", "--show-toplevel"):
                    return (str(root) + "\n").encode("utf-8")
                if arguments == ("cat-file", "-t", commit):
                    return b"commit\n"
                if arguments == ("cat-file", "-t", tree):
                    return b"tree\n"
                if arguments == (
                    "rev-parse",
                    commit + "^{tree}",
                ):
                    return (tree + "\n").encode("ascii")
                if arguments[:2] == ("cat-file", "blob"):
                    source_path = arguments[2][len(tree) + 1 :]
                    return blobs[source_path]
                raise AssertionError(arguments)

            with mock.patch(
                "release_tools.multisession_update_proof._git_output",
                side_effect=git_output,
            ):
                with self.assertRaisesRegex(
                    ProofError, "not bound to source tree path"
                ):
                    _candidate_source_binding(
                        manifest,
                        repository=root,
                        source_commit=commit,
                        source_tree=tree,
                    )

    def test_active_fingerprint_digest_binds_paths_bytes_and_modes(
        self,
    ) -> None:
        baseline = {
            "support/bin/manager": ("a" * 64, 0o755, 12),
            "external/codex-config": ("b" * 64, 0o600, 34),
        }
        reordered = {
            "external/codex-config": ("b" * 64, 0o600, 34),
            "support/bin/manager": ("a" * 64, 0o755, 12),
        }
        self.assertEqual(
            _fingerprint_sha256(baseline),
            _fingerprint_sha256(reordered),
        )
        changed_mode = dict(baseline)
        changed_mode["support/bin/manager"] = ("a" * 64, 0o700, 12)
        self.assertNotEqual(
            _fingerprint_sha256(baseline),
            _fingerprint_sha256(changed_mode),
        )

    def test_transaction_receipt_binds_both_versions_and_archive(
        self,
    ) -> None:
        archive_sha256 = "c" * 64
        result = {
            "ok": True,
            "action": "update",
            "previous_version": PRIOR_VERSION,
            "version": CANDIDATE_VERSION,
            "archive_sha256": archive_sha256,
            "transaction": {
                "action": "update",
                "previous_version": PRIOR_VERSION,
                "current_version": CANDIDATE_VERSION,
                "archive_sha256": archive_sha256,
                "receipt": "/disposable/receipt.json",
            },
        }
        transaction = _assert_transaction(
            result,
            action="update",
            previous_version=PRIOR_VERSION,
            current_version=CANDIDATE_VERSION,
            archive_sha256=archive_sha256,
        )
        self.assertEqual(
            "/disposable/receipt.json", transaction["receipt"]
        )
        result["transaction"]["previous_version"] = "0.4.0-rc.4"
        with self.assertRaisesRegex(ProofError, "receipt identity"):
            _assert_transaction(
                result,
                action="update",
                previous_version=PRIOR_VERSION,
                current_version=CANDIDATE_VERSION,
                archive_sha256=archive_sha256,
            )

    def test_registration_evidence_rejects_legacy_or_drift(
        self,
    ) -> None:
        block = (
            b"\n"
            b"[mcp_servers.Roblox_Studio_Multisession]\n"
            b'command = "/disposable/manager"\n'
        )

        class Module:
            @staticmethod
            def _expected_codex_block(_layout):
                return block

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            state = root / "install-state.json"
            config.write_bytes(b"# user\n" + block)
            os.chmod(config, 0o600)
            state.write_text(
                json.dumps(
                    {
                        "codex": {
                            "table": CANONICAL_SERVER_HEADER,
                            "block_sha256": hashlib.sha256(
                                block
                            ).hexdigest(),
                        }
                    }
                ),
                encoding="utf-8",
            )
            layout = SimpleNamespace(
                codex_config=config,
                install_state=state,
            )
            evidence = _registration_evidence(Module, layout)
            self.assertEqual(0o600, evidence["config_mode"])
            self.assertTrue(evidence["former_server_absent"])

            config.write_bytes(
                config.read_bytes()
                + b"\n[mcp_servers.Roblox_Studio_v2]\n"
            )
            with self.assertRaisesRegex(
                ProofError, "canonical registration"
            ):
                _registration_evidence(Module, layout)

    def test_output_paths_are_fresh_external_and_non_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "source"
            home = root / "home"
            artifacts = root / "artifacts"
            project.mkdir()
            home.mkdir()
            artifacts.mkdir()
            proof_input = artifacts / "candidate.tar.gz"
            proof_input.write_bytes(b"candidate")
            output = artifacts / "proof.json"
            resolved, checksum = _validated_output_paths(
                output,
                protected_inputs=(proof_input,),
                project_root=project,
                user_home=home,
            )
            self.assertEqual(output.resolve(), resolved)
            self.assertEqual(
                (artifacts / "proof.json.sha256").resolve(), checksum
            )

            for live_output in (
                project / "proof.json",
                home / ".codex" / "proof.json",
                home
                / "Documents"
                / "Roblox"
                / "Plugins"
                / "proof.json",
                home
                / "Library"
                / "Application Support"
                / "RobloxStudioMCPv2"
                / "proof.json",
            ):
                with self.subTest(live_output=live_output):
                    with self.assertRaisesRegex(
                        ProofError, "known live root|source"
                    ):
                        _validated_output_paths(
                            live_output,
                            protected_inputs=(proof_input,),
                            project_root=project,
                            user_home=home,
                        )

    def test_output_refuses_existing_or_input_colliding_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "source"
            home = root / "home"
            artifacts = root / "artifacts"
            project.mkdir()
            home.mkdir()
            artifacts.mkdir()
            proof_input = artifacts / "candidate.tar.gz"
            proof_input.write_bytes(b"candidate")
            existing = artifacts / "existing.json"
            existing.write_bytes(b"preserve")
            with self.assertRaisesRegex(ProofError, "fresh"):
                _validated_output_paths(
                    existing,
                    protected_inputs=(proof_input,),
                    project_root=project,
                    user_home=home,
                )
            with self.assertRaisesRegex(ProofError, "fresh|collides"):
                _validated_output_paths(
                    proof_input,
                    protected_inputs=(proof_input,),
                    project_root=project,
                    user_home=home,
                )
            self.assertEqual(b"preserve", existing.read_bytes())
            self.assertEqual(b"candidate", proof_input.read_bytes())

    def test_atomic_output_write_never_replaces_existing_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "proof.json"
            _atomic_write(target, b"first\n", 0o600)
            self.assertEqual(b"first\n", target.read_bytes())
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            with self.assertRaisesRegex(ProofError, "fresh"):
                _atomic_write(target, b"second\n", 0o600)
            self.assertEqual(b"first\n", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
