# Testing and release proof

## Complete local check

On a native Apple Silicon Mac:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -v
python3 -B scripts/validate_capability_parity.py
python3 -B scripts/release_dry_run.py
```

The unit suite uses only the Python standard library. HTTP tests bind an
ephemeral `127.0.0.1` port; a restrictive sandbox may require permission for
that loopback bind.

The release dry run:

1. verifies native `arm64` execution;
2. validates the exact 25-tool parity ledger and prerelease gate;
3. audits the repository;
4. builds the release twice from clean inputs and compares bytes;
5. audits the archive and its manifest;
6. installs into a temporary home and plugin/config tree;
7. runs status and doctor;
8. proves a no-op repair;
9. repairs a deliberately damaged v2-owned component;
10. simulates update and rollback;
11. uninstalls and verifies exact config/v1-sentinel restoration.

It never operates a real Studio place or changes the user's Codex, Roblox
plugin, or installed v1/v2 files.

Individual stages:

```bash
python3 -B scripts/audit_release.py --repo .
python3 -B scripts/build_durable_release.py --output-dir dist
python3 -B scripts/audit_release.py \
  --archive dist/roblox-studio-mcp-v2-0.4.0-rc.3-macos-arm64.tar.gz
python3 -B scripts/prove_release.py \
  --archive dist/roblox-studio-mcp-v2-0.4.0-rc.3-macos-arm64.tar.gz \
  --checksum-file dist/roblox-studio-mcp-v2-0.4.0-rc.3-macos-arm64.tar.gz.sha256
```

The generic proof's update fixture is deliberately synthetic. The release
gate additionally runs both real portable installers against the immutable
rc.4 restore bundle:

```bash
python3 -B scripts/prove_cross_version_rollback.py \
  --restore-bundle RESTORE_BUNDLE \
  --prior-archive RESTORE_BUNDLE/artifacts/roblox-studio-mcp-v2-0.3.0-rc.4-macos-arm64.tar.gz \
  --prior-checksum-file RESTORE_BUNDLE/artifacts/roblox-studio-mcp-v2-0.3.0-rc.4-macos-arm64.tar.gz.sha256 \
  --candidate-archive CANDIDATE_ARCHIVE \
  --candidate-checksum-file CANDIDATE_CHECKSUM \
  --candidate-expected-sha256 CANDIDATE_SHA256 \
  --candidate-version 0.4.0-rc.3 \
  --source-commit CANDIDATE_COMMIT \
  --source-tree CANDIDATE_TREE \
  --output EXTERNAL_ARTIFACT_DIR/CROSS_VERSION_ROLLBACK_PROOF.json
```

That proof SHA-checks the complete restore bundle and both archives before
creating its temporary home, installs the real rc.4 package, recreates a
reviewed catalog state through the real rc.4 import path, updates with the real
candidate installer, verifies rc.4 is the one-step rollback target, rolls back
with the candidate updater, and compares every active file byte and mode in
the transaction scope. Retained packages, backups, and transaction receipts
remain auditable expected history and are not misclassified as active-byte
drift. It then runs the restored rc.4 doctor and uninstall, proves exact
synthetic Codex/v1 restoration, and removes the temporary home.

A fresh disposable rc.4 install generates new credentials and therefore a
different rendered plugin from the live immutable plugin hash. The proof
compares that disposable rc.4 state to its own post-rollback bytes while
retaining the live rc.4 catalog/plugin hashes as provenance.

## Mandatory native rendered-plugin compilation

Before any live candidate validation, render and hash the exact candidate
`.rbxmx`, then run:

```bash
python3 -B scripts/native_studio_compile_smoke.py \
  --package EXACT_CANDIDATE_PLUGIN.rbxmx \
  --expected-package-sha256 EXACT_PLUGIN_SHA256 \
  --expected-source-sha256 EXACT_MAIN_SOURCE_SHA256 \
  --studio-executable /Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio \
  --expected-studio-executable-sha256 EXACT_STUDIO_SHA256 \
  --receipt EXACT_GATE_WORK_ROOT/native-studio-compile-proof.json
```

The gate strictly verifies the Studio app signature, Apple trust anchor,
exact Roblox bundle/signing-team requirement, and executable hash,
requires no running Studio process, extracts the sole exact `Main` source,
checks its reviewed pre-registration Edit/plugin-context fence, and runs it in
an empty command-script task. Success requires the identity-bearing sentinel
and the exact expected plugin-context assertion, with no compiler/register
error, unexpected user-plugin load, crash, or timeout. The receipt, exact
plugin bytes, and Studio executable are rehashed before a later live harness
may consume them. Missing Studio, an invalid signature, or absent/mismatched
evidence is an unqualified candidate, never a skipped pass.

The isolated read-only harness makes this proof mechanical. `prepare` must
run in a fresh private work root containing one exact extracted candidate
release. It binds the complete release manifest and extracted tree, every
runtime/config/secret file, the rendered package and sole `Main` source, the
port, and the exact public read-only scope:

```bash
python3 -B scripts/candidate_readonly_gate.py \
  --work-root EXACT_GATE_WORK_ROOT \
  prepare \
  --version 0.4.0-rc.3 \
  --release-manifest-sha256 EXACT_RELEASE_MANIFEST_SHA256 \
  --durable-catalog-sha256 EXACT_DURABLE_CATALOG_SHA256 \
  --port CANDIDATE_PORT

python3 -B scripts/native_studio_compile_smoke.py \
  --package EXACT_GATE_WORK_ROOT/StudioMCPv2CandidateReadOnly.rbxmx \
  --expected-package-sha256 EXACT_PLUGIN_SHA256 \
  --expected-source-sha256 EXACT_MAIN_SOURCE_SHA256 \
  --studio-executable /Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio \
  --expected-studio-executable-sha256 EXACT_STUDIO_SHA256 \
  --receipt EXACT_GATE_WORK_ROOT/native-studio-compile-proof.json

python3 -B scripts/candidate_readonly_gate.py \
  --work-root EXACT_GATE_WORK_ROOT qualify-native
python3 -B scripts/candidate_readonly_gate.py \
  --work-root EXACT_GATE_WORK_ROOT status
python3 -B scripts/candidate_readonly_gate.py \
  --work-root EXACT_GATE_WORK_ROOT start
```

`qualify-native` writes a new private qualification record and immediately
revalidates the receipt, package/source, logs, release/config provenance, and
the current signed executable at the fixed official Studio path. `start`,
discovery, direct calls, and job calls repeat that validation and have no
unqualified loader option. `status` never authenticates to a broker without
the proof. The exact cleanup `stop` path deliberately does not depend on the
receipt, plugin, catalog, state, or extracted payload remaining readable; it
uses only the candidate-owned private runtime identity and tokens plus the
broker-instance receipt pinned immediately after Start. It requires live
authenticated health and the local record to match that exact instance before
Stop. Substituted runtime credentials, a replaced broker, or a missing receipt
fail closed. The cleanup path exposes no client or operation capability.

The restore bundle's Git bundle is verified from repository context, for
example `git -C SOURCE_REPOSITORY bundle verify ABSOLUTE_BUNDLE_PATH`; the
bundle bytes and historical restore instructions remain immutable.

## Test matrix

| Invariant | Primary coverage |
|---|---|
| Every operational schema and job call requires `studio_id` | `test_explicit_targeting.py` |
| Missing, malformed, unknown, disconnected, or unauthorized IDs never fall back | `test_explicit_targeting.py`, `test_state_jobs_security.py` |
| Reads, writes, scripts, console, screenshot, input, Play/Stop, and jobs route only to the target | `test_explicit_targeting.py` |
| Different Studios overlap; same-Studio conflicts serialize | `test_concurrency.py` |
| More than two sessions use the same dynamic scheduler | `test_concurrency.py` |
| Correlation is session/document/generation scoped | `test_reconnect_and_correlation.py` |
| Reconnect rotates credentials, fences old work, and prevents replay | `test_reconnect_and_correlation.py` |
| Dispatched timeout uncertainty quarantines only the affected session | `test_reconnect_and_correlation.py` |
| Console, mode, jobs, and cancellation state are per-session | `test_state_jobs_security.py` |
| Loopback, distinct tokens, origin rejection, and request bounds | `test_http_and_mcp.py` |
| Play transition nonces, acknowledgements, watchdogs, reconnect fencing, and Edit proof | `test_play_bridge.py` |
| Durable plugin surface, fixed handlers, narrow input, and renderer restrictions | `test_durable_plugin.py` |
| Installer ownership, idempotency, crash-interrupted update recovery, config preservation, rollback, and uninstall | `test_durable_installer.py` |
| Native arm64-only rejection occurs before mutation | installer/platform tests |
| Catalog additions, incompatible shape quarantine, generation, atomic replacement, and rollback | `test_durable_plugin.py` |
| Repository/archive secrets and portability audit | release audit tests |
| Isolated install/update/rollback proof | release proof tests |
| Exact 25-tool set, valid route references, negative parity claims, and prerelease gating | `test_capability_parity.py` |
| Phase 2 tree schema, literal filters, cursor fencing, executable pagination model, traversal/output bounds, and upstream quarantine | `test_phase2_tree_contract.py`, `test_phase2_tree_luau.py`, `test_phase2_tree_pagination_model.py` |
| Phase 2 state predicate/context contract and malicious connected-result rejection | `test_phase2_state_contract.py`, `test_phase2_state_luau.py`, `test_phase2_state_response_validation.py` |
| Tree/state same-session serialization, cross-session isolation, and reconnect fencing | `test_phase2_tree_state_isolation.py` |
| Phase 2 script-name search and cross-script literal grep schemas, cursor domains, deterministic pagination, traversal/source/output bounds, upstream quarantine, and malicious connected-result rejection | `test_phase2_script_catalog_contract.py`, `test_phase2_script_luau.py`, `test_phase2_script_pagination_model.py`, `test_phase2_script_response_validation.py` |
| Script search/grep same-session serialization, cross-session isolation, reconnect fencing, and explicit-target stripping | `test_phase2_script_isolation.py` |
| Phase 2 detailed instance inspection schema, fixed value codec, bounded tree summaries, malicious-result rejection, same-session FIFO, cross-session overlap/isolation, and reconnect-generation fencing | `test_phase2_instance_inspect_contract.py`, `test_phase2_inspect_luau.py`, `test_phase2_inspect_response_validation.py`, `test_phase2_inspect_isolation.py` |
| Revision-protected multi-edit normalization, deterministic edit order, UTF-8/range/overlap rejection, target/source/receipt bounds, all-target prepare, CAS apply, compensation, exact recovery, and no cross-script atomicity claim | `test_phase2_multi_edit_model.py`, `test_phase2_multi_edit_luau.py`, `test_phase2_multi_edit_session_integrity.py` |
| Closed direct/nested-job input validation, frozen admitted arguments, exact job allowlist, handler output validation, and identity/schema/result receipts | `test_phase2_input_schema_enforcement.py`, `test_phase2_output_schema_parity.py`, `test_phase2_job_state_parity.py` |
| Same-session direct/job FIFO, cross-session overlap/isolation, reconnect-generation fencing, bounded job/result retention, and hash-chained terminal tombstones | `test_phase2_job_fifo_isolation.py`, `test_phase2_job_retention_audit.py`, `test_phase2_multi_edit_session_integrity.py` |
| Cross-file/package release-version coherence and exact real rc.4→candidate→rc.4 active-byte/mode proof boundaries | `test_release_version_coherence.py`, `test_cross_version_rollback_proof.py`, external `prove_cross_version_rollback.py` evidence |

## Explicit-targeting contract

The catalog tests iterate over every exposed Studio operation. Each schema must
place a UUID-formatted `studio_id` in its required fields. The routing tests
exercise zero, one, two, and more connected sessions and prove that omission
never selects the only available Studio.

The catalog update fixture includes a simulated upstream-added tool. A
compatible exact mapping can regenerate a reviewed durable schema. An unknown
family, alias, or changed argument shape remains quarantined and cannot enter
the published MCP catalog.

Crash-recovery coverage includes a genuinely fresh Python process with no
in-memory transaction nonce, interrupted update and rollback, candidate-version
and corrupt install state, wrong acknowledgements, tampered and out-of-root
snapshots, a lifecycle stop refusal that changes no owned bytes, and a crash
during restore/doctor followed by a successful resumable `repair`. The pending
marker must be removed last.

Atomic `bin` restoration is interrupted immediately before and after every
file replacement; the manager must remain a regular file in every case, and a
fresh process must finish the restore. Separate tests prove journal
create/unlink parent-directory fsync calls, marker restoration after an unlink
fsync refusal, fail-closed installer metadata fsync, direct cross-version
install refusal, recovery-nonce non-authorization, and the exact live update
nonce/from/to authorization path. The sequential release test covers
A→B→C followed by C→B rollback with the target snapshot's receipt.

Verify-before-execute sentinels rewrite retained `install.py` and its manifest
and add an import-shadow module to the installed release tree. The rollback
target manifest must match the digest anchored in its exact snapshot; no
tampered installer module or lifecycle subprocess may execute.

## Play/Stop contract

Play bridge tests cover:

- exact binding to the pending targeted start request;
- two-phase `starting` and `stopping` acceptance without mutation replay;
- full Studio/plugin/document/generation/request/place/game/nonce context;
- one-time server attach and derived server credentials;
- delayed attachment receiving a fresh bounded active lifetime;
- same-session lifecycle serialization and cross-session independence,
  including both sessions ready, first-session Edit completion while the
  second remains in Play, and ordered second-session completion;
- `watchdog_armed` before ready;
- exact `stop_received` acknowledgement before `EndTest`;
- replayed or mismatched transition data rejection;
- disconnect/reconnect recovery fencing;
- disconnected read-only broker recovery state;
- bounded watchdog state without false completion;
- runner return, exact temporary Script cleanup, and stable Edit observations.
- plugin-context PlayServer attachment with an inert disabled marker, without
  writing the place's saved HTTP setting;
- conservative terminal disconnected-session retention and audited
  compaction, with uncertain or active records never retired.

## Plugin validation

Renderer tests deterministically compose the plugin from:

- `scripts/studio_plugin_template.luau`;
- `scripts/play_server_bridge.luau`;
- `scripts/durable_operation_handlers.luau`.

They verify placeholder replacement, fixed loopback URLs, dynamic window-local
identity, exact capability/handler agreement, no global Studio selector, no
arbitrary execution primitives, and the lifecycle acknowledgement ordering.
Credential-bound `.rbxmx` output is generated only during local installation
and is never committed or shipped in the portable archive.

## Held two-place rc.4 acceptance

This live gate is intentionally separate from package validation. Restart
Codex and reload or restart Roblox Studio before beginning so both use the
installed rc.4 broker and plugin.

Use only Roblox Studio MCP v2. Start with `list_roblox_studios_v2`, resolve
exactly `Experiments` and
`Workshop`, and record each returned
`studio_id`, `place_id`, `game_id`, and `document_epoch`. Every later call must
carry the corresponding explicit `studio_id`; never use an active or default
Studio target.

The ordered gate is:

1. Positively observe both exact sessions in Edit.
2. Send Start once to Experiments. Record its correlated `starting` receipt;
   do not retry Start. Observe that exact session until it reports Play.
3. Send Start once to Workshop. Record its correlated `starting` receipt;
   do not retry Start. Observe that exact session until it reports Play.
4. Read both exact session states and positively confirm both are
   simultaneously in Play.
5. Send Stop once to Experiments. Record its correlated `stopping` receipt,
   then observe Experiments in Edit while a fresh read still reports Workshop
   in Play.
6. Send Stop once to Workshop, then positively observe Workshop in
   Edit.

Do not modify content, save, or publish. A timeout or disconnect is not
permission to replay a mutation. Inspect both explicit v2 session states and
use only a correlated safe Stop path when the observed state authorizes it.
Otherwise stop the gate and retain the failure evidence.

The acceptance report must include exact place identities, the two
`studio_id` values, transition nonces or request correlations from acceptance
receipts, each positive state observation, stop order, final states, and any
timeout, disconnect, or recovery action.

## Live-validation boundary

Pull-request and release CI do not launch Roblox Studio. Live Play/Stop testing
requires user-authorized disposable places and interactive Studio permissions.

The unchanged 0.2.0 operation surface was previously validated with two
disposable Studio sessions: isolated concurrent edits, a single-session
Play/Stop peer-isolation gate, concurrent two-session Play/Stop, observed Edit
return, and cleanup all passed. The GitHub release-candidate proof covers the
new distribution, architecture, update, and rollback code without touching any
place.

Any future change to Studio-side handlers or lifecycle source requires a fresh
ordered live gate: one disposable session must start, stop, return to Edit, and
clean up before a second session may be tested concurrently.

## GitHub Actions

CI runs only on GitHub-hosted Apple Silicon macOS labels and asserts
`uname -m` is `arm64`. No Intel or universal artifact is built. The matrix uses
Python 3.11 and 3.13 because GitHub removed Python 3.9 and 3.10 from its arm64
macOS tool cache. The supported Python 3.9 minimum is exercised separately on
a native Apple Silicon installation with Python 3.9 available.

The release workflow runs for a manually selected exact version tag. It uses a
protected `github-release` environment for the only job with
`contents: write`; branch builds cannot publish.
