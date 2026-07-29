# Roblox Studio MCP v2 0.4.0-rc.1

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This is the first isolated Phase 2 release candidate. It has not been installed
or exercised against a live Studio place. The installed `0.3.0-rc.4`
integration and its checksum-manifested restore bundle remain unchanged and
are the required immediate rollback target for any separately authorized
candidate installation. The portable candidate manifest contains exactly 33
allowlisted files, including the multi-edit and schema-validation runtime
modules.

## Revision-protected multi-edit

- Adds `studio_multi_edit_v2` for 1-16 existing exact
  `LuaSourceContainer` paths. Every target requires its expected lowercase
  source SHA-256.
- Preflights every path, revision, ordered edit, expanded replacement span,
  UTF-8 boundary, source limit, and final revision before the first write.
- Uses deterministic target-input/edit-input order and per-target
  `UpdateSourceAsync` compare-and-swap with read-back acknowledgement.
- Rejects duplicate or ambiguous targets, stale revisions, overlapping
  matches, invalid ranges/UTF-8, and independently bounded target, edit,
  literal, source, argument, span, and receipt payloads.
- Does not claim cross-script atomicity. Partial dispatch attempts bounded
  compensating rollback only under an exact planned-revision CAS.
- Adds `studio_recover_multi_edit_v2`, which accepts only the original
  transaction UUID under the same explicit Studio, client, document, and
  generation identity. Unprovable work remains quarantined and is never
  replayed.

## Direct and job contract parity

- Validates direct and nested-job arguments against the selected immutable
  closed handler schema before dispatch or job creation.
- Freezes admitted arguments and records exact Studio/client/document/
  generation identity, handler and input/output schema hashes, argument hash,
  phase request IDs, cancellation state, terminal outcome, and result digest.
- Restricts public jobs to the accumulated tree, state, script-name search,
  literal grep, instance-inspection, multi-edit, and exact-recovery handlers
  with complete host result validators.
- Serializes direct calls and jobs through one FIFO admission lane per session
  while preserving cross-session concurrency.
- Preserves original uncertain apply evidence when exact recovery completes by
  appending a validated resolution receipt rather than overwriting the
  original request or result provenance.
- Bounds active jobs, retained jobs, and retained result bytes. Only positively
  terminal records may retire into bounded hash-chained non-secret tombstones;
  active, uncertain, and recovery-required evidence is never compacted.

## Independent hardening findings

- Exact recovery now fails before dispatch when a reconnect changes the
  session generation; the broker marker, every uncertain request/pending
  record, stored prepare receipt, and Studio plan must all share one
  generation.
- Apply CAS cannot treat an externally changed source that already equals the
  planned bytes as this transaction's successful write. Idempotent
  already-restored handling is limited to compensating rollback and explicit
  recovery.
- Generic input-schema validation handles arbitrarily large Python integers
  without float-overflow exceptions and keeps nested JSON booleans distinct
  from integers in `const`/`enum` comparisons.

## Preserved boundaries

Every operational v2 tool still requires explicit `studio_id`; discovery is
the only exception. There is no active/default Studio route and no silent v1
fallback. This candidate adds no arbitrary Luau, host filesystem/process,
caller-selected URL, asset upload/insertion, or saved place-setting surface.
Tree, state, script discovery, and instance-inspection parity remain bounded
and honestly partial where the official contracts differ.

The exact 25-tool parity ledger still reports 12 partial or deferred P0 gaps.
Any live read-only validation or installation requires a separate user gate.
