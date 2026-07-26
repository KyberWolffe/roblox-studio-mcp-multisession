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
  --archive dist/roblox-studio-mcp-v2-0.3.0-rc.2-macos-arm64.tar.gz
python3 -B scripts/prove_release.py \
  --archive dist/roblox-studio-mcp-v2-0.3.0-rc.2-macos-arm64.tar.gz \
  --checksum-file dist/roblox-studio-mcp-v2-0.3.0-rc.2-macos-arm64.tar.gz.sha256
```

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
- full Studio/plugin/document/generation/request/place/game/nonce context;
- one-time server attach and derived server credentials;
- same-session lifecycle serialization and cross-session independence;
- `watchdog_armed` before ready;
- exact `stop_received` acknowledgement before `EndTest`;
- replayed or mismatched transition data rejection;
- disconnect/reconnect recovery fencing;
- bounded watchdog state without false completion;
- runner return, exact temporary Script cleanup, and stable Edit observations.

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
