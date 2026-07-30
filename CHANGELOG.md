# Changelog

This project uses semantic versioning for the broker, installer, and Studio
plugin release. The reviewed upstream Roblox tool-catalog version is tracked
separately and is reported by `doctor`.

## 0.4.0-rc.5 — unreleased

Renamed the public product and Codex registration to Roblox Studio MCP
Multisession without changing the v2 protocol, tool, or physical installation
identities that rc.4 recovery depends on.

### Canonical public identity

- Uses `Roblox Studio MCP Multisession` as the human product name,
  `Studio MCP Multisession` as the short display name,
  `Roblox_Studio_Multisession` as the Codex server name, and
  `roblox-studio-mcp-multisession` as the repository/release slug.
- Transactionally replaces the exactly owned former
  `[mcp_servers.Roblox_Studio_v2]` registration. Installation, doctor, repair,
  update, rollback, and uninstall reject a dual-active or unowned ambiguous
  configuration instead of guessing.
- Adds the canonical Codex configuration example and retains the old-name
  example only as a disabled compatibility/migration reference.

### Rc.4 migration and rollback bridge

- Preserves `_v2` public tool names, `/v2` routes, Python package/internal
  identifiers, the `RobloxStudioMCPv2` support root,
  `StudioMCPv2SideBySide.rbxmx`, legacy launcher/manager names, archive
  basename, and manifest identity.
- Retains legacy artifact filenames deliberately because the immutable rc.4
  bootstrap, update journal, package manifests, and byte-for-byte one-step
  rollback verify those exact names.
- Captures and hash-binds the complete pre-migration Codex configuration,
  activates exactly one canonical registration, and keeps the installed
  `0.4.0-rc.4` release as the immediate rollback target.
- Does not widen tools, schemas, permissions, routing, or fallback behavior.
  Every operational tool still requires explicit `studio_id`; discovery is
  the only exception, and silent v1 fallback remains forbidden.

## 0.4.0-rc.4 — unreleased

Corrected the Phase 2 native qualification policy while preserving the
installed `0.3.0-rc.4` integration and restore bundle as the immediate
rollback target.

### Proportional native qualification

- Binds the exact Roblox Studio main-executable hash, Info.plist version/build
  and bundle executable, and the narrow Apple/Roblox signing identity and
  CDHash.
- Treats full-bundle `codesign --verify --deep --strict` as diagnostic and
  provenance evidence rather than a hard functional prerequisite.
- Compiles the exact rendered plugin's sole `Main` source and links that
  evidence to a future actual candidate-plugin load/registration check with
  clean logs.
- Requires bounded read-only checks through explicit `studio_id` routing
  before any later installation decision. No active/default routing, mutation,
  Play, save, publish, or v1 fallback is introduced.

## 0.4.0-rc.3 — superseded 2026-07-29

Corrected native-qualification identity for the isolated Phase 2 candidate.
It is preserved as superseded because full-bundle deep/strict verification was
incorrectly made a hard functional prerequisite. The rc.1 rejection and rc.2
superseded checkpoint remain immutable, and the installed `0.3.0-rc.4` bytes
and restore bundle remain the immediate rollback target.

### Native Studio identity

- Corrected the exact official Studio bundle/signature identifier from the
  incorrectly cased `com.roblox.RobloxStudio` expectation to
  `com.Roblox.RobloxStudio`.
- Pinned the signed TeamIdentifier to Roblox's exact `2CFABCH843` identity and
  advanced the receipt format so evidence from the weaker identity contract
  cannot be reused.
- Required an Apple-anchored code-signing requirement containing that exact
  bundle identifier and leaf-certificate team OU, preventing a locally
  self-signed metadata imitation from satisfying the gate.
- Kept strict code-signature verification mandatory. An unsigned, modified,
  or otherwise unverifiable Studio app still blocks native qualification and
  cannot be treated as a skipped or partial pass.
- Preserved the exact-package/source/executable hash binding, empty Edit
  command-script guard, reviewed assertion outcome, bounded evidence, and
  receipt revalidation introduced for rc.2.

### Preserved candidate corrections

- Retains the rendered-plugin register-budget refactor and official Luau
  compiler coverage.
- Retains public `*_v2` read-only gate names, explicit `studio_id` routing,
  complete extracted-tree provenance, exact broker-instance cleanup fencing,
  and all rc.1 workflow/CAS/recovery behavior.

## 0.4.0-rc.2 — superseded 2026-07-29

Corrected isolated Phase 2 candidate. The failed rc.1 commit and artifact
remain immutable and are rejected; the installed `0.3.0-rc.4` bytes and
restore bundle remain the immediate rollback target.

### Rendered-plugin compilation

- Moved the durable handler registry into a private frozen factory/facade and
  grouped immutable services, bounds, and protocol metadata. The handler
  factory now stays below a conservative 160-local budget instead of exceeding
  Luau's 200-local compiler limit.
- Added static scope-budget drift checks and a mandatory receipt-bound native
  Studio compilation gate for the exact hashed `.rbxmx` source before any
  future live validation.
- The native gate verifies the signed Studio executable and empty Edit
  command-script context, accepts only the reviewed pre-registration
  plugin-context assertion, and fails closed on compiler errors, unexpected
  plugin loads, crashes, timeouts, or identity drift.

### Candidate live-gate boundary

- Kept public `*_v2` tool names intact through direct calls, authorization, and
  job admission. Remote base handler names are now catalog-audit-only.
- Added exact public/remote bijection, explicit-target injection, read-only
  annotation, candidate-origin, file-hash/mode, and bounded argument checks to
  the isolated read-only harness.
- Bound the complete extracted release tree and all runtime/config/secret
  bytes into native qualification, removed the internal unqualified
  capability loader, pinned revalidation to the official Studio path, and
  separated proof-independent authenticated cleanup.
- Required an external release-manifest hash before candidate import and
  fenced cleanup to the exact broker instance receipted at Start.

## 0.4.0-rc.1 — rejected 2026-07-29

First isolated Phase 2 release candidate. The installed `0.3.0-rc.4` bytes and
restore bundle remain immutable and are the required immediate rollback
target. No live Studio or installation action is part of this candidate build.

### Revision-protected multi-edit

- Added bounded all-target prepare, deterministic ordered per-target CAS
  apply, read-back acknowledgements, compensating rollback, and exact
  transaction recovery for existing script sources.
- Required exact source SHA-256 revisions and rejected stale, duplicate,
  ambiguous, overlapping, invalid UTF-8/range, or unbounded plans before
  mutation.
- Preserved honest non-atomic semantics: unprovable partial dispatch is
  quarantined and cannot be retried or rebound across reconnect generations.

### Direct/job state and result contracts

- Added fail-closed selected-handler input validation for direct and nested job
  calls, immutable admitted argument snapshots, and closed result validators
  for every job-admissible workflow operation.
- Added identity/schema/argument/request/result receipts, one same-session FIFO
  lane for direct and job work, cross-session isolation, bounded result
  retention, and hash-chained terminal tombstones.
- Exact recovery appends a validated resolution receipt without overwriting
  the original uncertain apply evidence.

### Hardening

- Fenced broker and Studio recovery to the exact original generation.
- Made apply CAS strict against an externally pre-existing planned revision;
  idempotence remains limited to rollback and recovery.
- Hardened huge-integer and nested JSON equality handling in catalog input
  validation.

## 0.4.0-dev.3 — unreleased

Phase 2 installer/update hardening on the isolated parity line. The immutable
`0.4.0-dev.2` commit and artifacts remain unchanged, and the installed
`v0.3.0-rc.4` checkpoint remains the immediate guarded rollback target.

### Guarded catalog-contract migration

- Authorized cross-version updater transactions now replace the durable
  catalog pair, upstream snapshot pair, and compatibility manifest with the
  candidate package's prevalidated defaults. Active catalog bytes reviewed for
  an older release are never implicitly carried or rebased into a new release.
- Existing catalog-review receipts and audit records remain intact. A user may
  explicitly review and import a compatible upstream snapshot again after the
  candidate is accepted.
- Same-version repair retains its existing ownership semantics: an intact
  state-owned reviewed copy repairs its peer, while two drifted copies fall
  back to the same-version packaged default.
- Installed doctor checks now cover both durable mirrors, both upstream
  mirrors, the compatibility manifest, their ownership hashes, and the
  installed release source. Transaction snapshots and one-step rollback retain
  exact pre-switch bytes.
- Candidate preflight validates the exact updater snapshot first, then requires
  all live contract/state bytes to match it before and after lifecycle stop.
  First install and uninstall use a stable parent-level lock that survives
  support-root moves; owned installs additionally take the legacy in-root lock.
  Install/repair and catalog import/rollback use the same ordering.

## 0.4.0-dev.2 — unreleased

Phase 2 instance-inspection development on the isolated parity line. The
immutable `v0.3.0-rc.4` restore checkpoint and installed bytes remain
unchanged, and rc.4 remains the immediate guarded rollback target.

### Phase 2 instance-inspection slice

- Added `studio_inspect_instance`, an Edit-only exact-path observation
  operation with a fixed 34-selector safe property allowlist.
- Properties and attributes use a closed, bounded value encoding. Tags,
  immediate children, and descendant class counts are deterministically
  ordered and bounded by explicit depth, scan, time, and output budgets.
- Host validation rejects authenticated responses that the fixed Studio codec
  cannot produce, including incompatible selector groups, malformed value
  encodings, unsafe truncated-child paths, and incoherent traversal counts.
- Input and output schemas are pinned independently in the compatibility
  manifest. The official `inspect_instance` alias remains unexposed and its
  broader dot-path, multi-match, reflection, and `UniqueId` behavior remains
  review-only and incompatible.
- Parity remains honestly partial and all operational routes continue to
  require explicit `studio_id`; no global/default route or silent v1 fallback
  was added.

## 0.4.0-dev.1 — unreleased

Phase 2 capability-parity development based on the immutable
`v0.3.0-rc.4` restore checkpoint.

### Development policy

- All parity work remains isolated from the installed rc.4 integration.
- Every Studio-bound operation continues to require explicit `studio_id`;
  discovery remains the only exception.
- New upstream schemas remain review-only until their bounded durable
  handlers, conflict semantics, and security tests are complete.
- A future installable candidate must retain rc.4 as its immediate guarded
  rollback target.

### Phase 2 tree and state slice

- The durable catalog and exact-schema compatibility manifest advance to the
  isolated `0.4.0-dev.1` line; the frozen rc.4 catalog bytes remain unchanged.
- `studio_list_tree` retains its exact segment-array path contract and adds
  bounded literal name and class filters, deterministic iterative traversal,
  scan/page/output limits, and opaque session-, generation-, query-, and
  lineage-fenced continuation cursors.
- Durable catalog review now pins every handler to its exact closed argument
  schema; incompatible or unknown upstream shapes remain review-only and are
  never auto-enabled.
- `studio_get_state` now distinguishes normalized Play-transition state from
  raw controller predicates and reports only the actually routable Edit
  DataModel channel. The PlayServer lifecycle bridge is not presented as a
  general Server or Client request channel.
- Connected durable state results are identity- and generation-checked on the
  host before they can update cached mode. Disconnected recovery reports no
  available request channel and never infers Server or Client routability.

### Phase 2 script discovery slice

- Added `studio_search_scripts`, a deterministic cursor-paginated
  script-name search with exact reusable paths, bounded literal-subsequence
  keywords, and session/document/generation/query/lineage fencing.
- Added `studio_grep_scripts`, an Edit-only literal cross-script grep with
  source-byte, scan, result, output, and time budgets. Mid-script cursors bind
  the exact source revision, and returned previews preserve UTF-8 boundaries.
- Successful script-search and grep results use closed identity-bearing output
  contracts and are validated by the host before delivery or job retention.
- Exact root resolution now shares the 10,000-child width bound, and script
  time budgets include resolution and cursor reconstruction. Host validation
  additionally enforces deterministic path order, non-overlapping grep
  matches, and internally coherent line/preview metadata.
- Compatibility review now pins both input schemas and declared-or-absent
  output schemas for every durable handler. Upstream output-shape drift is a
  distinct fail-closed catalog change; non-finite JSON numbers are rejected.
- Official `script_search` and `script_grep` remain honestly partial: the
  upstream catalog does not specify fuzzy ranking/output semantics, and v2
  does not execute caller-supplied Luau patterns.

## 0.3.0-rc.4 — 2026-07-28

PlayServer HTTP-independence and terminal-session lifecycle correction.

### Fixed

- The guarded Play bridge now runs in the installed Studio plugin's
  PlayServer context. The temporary server Script is an inert,
  transition-owned marker, so a place with `Allow HTTP Requests` disabled can
  still attach to the fixed loopback broker without changing or saving the
  place setting.
- Bridge failures carry a bounded structured code through the correlated
  `EndTest` receipt instead of collapsing every pre-broker failure into
  `attach_failed`.
- Disconnected records are nonblocking only when Edit, zero in-flight work,
  zero uncertainty, and terminal-or-absent Play are all positively proven.
  Those records retain a reconnect grace period, then compact into bounded
  non-secret audit tombstones. Uncertain and active records are never retired.

## 0.3.0-rc.3 — 2026-07-26

Two-phase Play lifecycle and multi-window identity correction.

### Fixed

- Start and Stop now return correlated acceptance receipts without holding the
  mutation call open while Roblox Studio changes modes. Callers positively
  observe the exact session through `starting`, `play`, `stopping`, and Edit.
- Slow Play startup has separate bounded pre-attach, activation, active, and
  server-watchdog lifetimes. A successful server acknowledgement refreshes the
  active lifetime instead of consuming it during Studio startup.
- Broker-owned transition state remains available for read-only recovery
  observation when the targeted Studio controller disconnects.
- Studio discovery and state reads expose the same normalized Play transition
  states.
- Place identity uses the published asset name when available, avoiding
  misleading runtime DataModel names in multi-window discovery.

## 0.3.0-rc.2 — 2026-07-26

Security and CI correction to the unpublished `0.3.0-rc.1` candidate.

### Fixed

- Authenticated loopback HTTP now uses a direct, proxy-free opener and refuses
  redirects. Ambient proxy variables can no longer receive bearer credentials
  or prevent local broker readiness.
- The disposable mock Studio applies the same loopback-only, proxy-free,
  no-redirect transport boundary.
- Broker binding bypasses Python's unnecessary reverse-DNS lookup for an
  already validated literal loopback address, so slow local name resolution
  cannot exhaust the bounded startup deadline.
- GitHub workflows suppress bytecode for every project Python command and
  audit the checkout before and after tests, preventing `__pycache__` from
  contaminating deterministic source audits.

## 0.3.0-rc.1 — 2026-07-26

Unpublished initial release-candidate packaging of the live-validated 0.2.0
multi-session architecture.

### Added

- Apple Silicon macOS-only platform gate, including fail-before-mutation
  detection for Intel Macs and processes running through Rosetta.
- Standalone, reproducible GitHub release packaging and SHA-256 manifests.
- Pinned-release bootstrap, staged update, retained-version rollback, and
  offline/private-repository update paths.
- Repository and archive audits for credentials, private runtime material,
  machine-specific paths, and unsafe package entries.
- Isolated temporary-home proof covering install, status/doctor, no-op repair,
  damaged-component repair, update/rollback, and uninstall restoration.
- Apple Silicon GitHub Actions CI and manually gated tag-release automation.
- Exact 25-tool v1 capability parity ledger, with an enforced prerelease gate
  while P0 gaps remain and no global v1 fallback.

### Preserved from 0.2.0

- Explicit `studio_id` on every Studio operation, with no active/default
  Studio fallback.
- Per-session locking, mode, console, job, correlation, reconnect, and
  lifecycle state.
- Concurrent operation of distinct Studio sessions and serialization of
  conflicting work within one session.
- Authenticated Play/Stop bridge with transition nonces, replay protection,
  bounded watchdogs, and observed return to Edit mode.
- Side-by-side Codex and Roblox Studio installation that leaves v1 untouched.
- Review-gated upstream catalog compatibility workflow.

### Compatibility

- Supported: native Apple Silicon macOS (`arm64`) with Python 3.9 or newer.
- Unsupported: Intel Macs, `x86_64` processes, and Rosetta execution.

## 0.2.0 — validated baseline

The durable broker, explicit-targeting MCP frontend, Studio plugin, installer,
catalog workflow, and Play/Stop lifecycle were completed and validated in an
isolated R&D environment. Version 0.3.0-rc.1 changes distribution and lifecycle
management; it does not widen the Studio operation surface.
