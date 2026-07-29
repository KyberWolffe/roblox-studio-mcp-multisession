# Roblox Studio MCP v2 0.4.0-dev.3

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This is an unreleased, development-only Phase 2 line. It preserves the exact
`0.4.0-dev.2` source and artifact identity while correcting the guarded update
path discovered by the isolated rc.4-to-development acceptance proof. The
installed `v0.3.0-rc.4` integration and its rollback bundle remain unchanged.

During an authorized, nonce-fenced cross-version transaction, the candidate
now installs its prevalidated durable catalog pair, upstream snapshot pair, and
compatibility manifest as one versioned contract. It never carries forward or
rebases active catalog bytes that were reviewed under the older release.
Existing review receipts and audit records are retained for provenance, but
they do not authorize candidate bytes; a user may explicitly re-review and
import after the update succeeds.

Same-version repair remains deliberately different. It preserves an exact
state-owned reviewed pair when either mirror is intact and restores the
same-version packaged default only when both mirrors are missing or drifted.
Installed doctor checks cover all five active catalog-contract files and their
ownership hashes, including the candidate release's compatibility-manifest
source. The updater's exact pre-switch snapshot remains the authority for
failure recovery and one-step rollback.

Candidate preflight validates the retained prior release and the exact captured
catalog/state bytes, then requires the live files to remain byte-identical
before and after lifecycle stop. A stable parent-level lock serializes first
install and remains held while uninstall moves the support root. Existing
owned installs take that lock first and then the legacy in-root lock, preserving
interoperation with retained releases. Install/repair, catalog import/rollback,
update, rollback, and uninstall share that ordering. Fresh management commands
fail before creating either support-root residue or a coordination lock.

Releases older than dev.3 did not lock their catalog administration commands;
an operator must not deliberately launch an old retained catalog command
concurrently inside the final checked-to-write interval of an upgrade. The
candidate still rechecks after stop and its doctor/snapshot recovery fails
closed if a resulting mismatch is observed.

The bounded instance-inspection capabilities introduced in dev.2 are carried
forward unchanged. Every operational v2 tool still requires explicit
`studio_id`; discovery remains the only exception. No global/default routing
or silent v1 fallback is added.
