# Rc.5 to rc.6 isolated update and rollback proof

`scripts/prove_multisession_update_rollback.py` is the narrow real-package
release gate for `0.4.0-rc.6`. It is separate from, and does not change, the
historical rc.4-to-rc.5 rename/migration proof.

The proof pins the published `0.4.0-rc.5` archive SHA-256
`d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d`
and its exact release manifest, installer, updater, bootstrap, source commit,
and source tree. It then:

1. audits and extracts the real rc.5 and selected rc.6 archives;
2. verifies the claimed rc.6 Git commit exists, is a commit, resolves to the
   claimed tree, and that every file receipt in the archive's closed build
   manifest matches the corresponding exact blob in that tree;
3. installs rc.5 into a disposable synthetic home and runs doctor;
4. updates transactionally through the real rc.5 updater;
5. runs the installed rc.6 doctor and proves rc.5 is the immediate rollback;
6. rolls back through the real rc.6 updater;
7. compares every active owned file byte, mode, and canonical Codex
   registration with the pre-update rc.5 fingerprint;
8. runs the restored rc.5 doctor and removes the disposable proof root.

Lifecycle calls receive bounded stopped acknowledgements. No broker, network,
Studio process, live Codex configuration, live plugin, or place is touched.
Retained packages, snapshots, and receipts are transaction history and are not
part of the active-byte comparison.

After the exact rc.6 candidate archive and source checkpoint exist, run:

```bash
python3 -B scripts/prove_multisession_update_rollback.py \
  --prior-archive ../candidate-artifacts/0.4.0-rc.5-9234222/roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz \
  --prior-checksum-file ../candidate-artifacts/0.4.0-rc.5-9234222/roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz.sha256 \
  --candidate-archive EXACT_RC6_ARCHIVE \
  --candidate-checksum-file EXACT_RC6_ARCHIVE.sha256 \
  --candidate-expected-sha256 EXACT_RC6_ARCHIVE_SHA256 \
  --candidate-version 0.4.0-rc.6 \
  --source-commit EXACT_RC6_COMMIT \
  --source-tree EXACT_RC6_TREE \
  --output EXTERNAL_ARTIFACT_DIR/MULTISESSION_RC5_RC6_ROLLBACK_PROOF.json
```

The output and adjacent checksum paths must both be fresh and outside the
source repository and the current user's known live Codex, Studio Plugins, and
`RobloxStudioMCPv2` installation roots. They must not collide with any archive
or checksum input. The command creates (and never replaces) a mode-`0600` JSON
receipt and adjacent checksum file. Any Git identity/source binding, archive,
manifest, version, receipt, rollback-target, registration, byte, or mode
mismatch fails closed.
