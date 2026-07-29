# Design provenance and source limits

## What was inspected

The original R&D work inspected an installed Roblox Studio MCP integration
read-only:

- its Codex MCP registration;
- the signed native Studio MCP executable shipped with Roblox Studio;
- its user-owned cached tool schemas;
- an older readable local Studio plugin.

That inspection established that the existing operational surface selected an
active Studio and did not require `studio_id`. It also established that the
readable legacy plugin was not the source for the full native tool catalog.

This public repository intentionally contains none of those machine-specific
paths, hashes, caches, plugins, credentials, logs, live session state, or
place identifiers.

## Source limitation

The production proxy's Rust source and its current native Studio-side
implementation were not available. Binary metadata and cached schemas were
enough to identify the selection-based routing risk, but not enough to safely
reproduce a private wire protocol.

V2 therefore does not wrap or emulate the proprietary implementation. It is a
separate, auditable broker and Studio plugin with its own endpoint, credentials,
protocol, package name, and Codex registration.

The upstream schema snapshot in `config/tool-catalog.json` is compatibility
input only. It is not an operational allowlist, and importing a future upstream
catalog cannot expose a new operation without an operator-owned adapter and
tests.

## Supported Studio lifecycle APIs

The Studio plugin uses documented Plugin Security APIs. Roblox documents
`RunService` lifecycle controls and mode predicates, plus
`StudioTestService:ExecutePlayModeAsync()` and the server-DataModel
`StudioTestService:EndTest()` path.

The v2 runner uses the yielding test API because it can pass an immutable
transition bootstrap into the Play server and return a correlated result to
the original Edit-context plugin. Completion still requires server
acknowledgement, runner return, exact Script cleanup, and observed Edit mode.

- [Roblox RunService reference](https://create.roblox.com/docs/reference/engine/classes/RunService)
- [Roblox StudioTestService reference](https://create.roblox.com/docs/reference/engine/classes/StudioTestService)

## Distribution audit

The release builder packages an explicit file allowlist rather than traversing
the repository. A separate audit checks both the candidate tree and final
archive for:

- secrets and rendered bearer credentials;
- user-specific absolute paths;
- private run manifests, live state, logs, backups, and receipts;
- unsafe or duplicate archive paths;
- unexpected files outside the release allowlist.

The isolated proofs install only under temporary synthetic homes. Phase 2
changes the Studio operation surface, so package proof is not presented as a
substitute for live validation. Any live read-only validation or installation
of a candidate remains a separate, explicit user-authorized gate; ordinary
development, deterministic build, update/rollback proof, and archive audit do
not contact Studio or mutate the live rc.4 installation.
