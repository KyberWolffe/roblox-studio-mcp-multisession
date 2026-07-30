# Roblox Studio MCP Multisession 0.4.0-rc.6

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

Version `0.4.0-rc.6` is an isolated, unpublished script-lifecycle candidate
based on the exact published rc.5 source line. It does not change the installed
`0.4.0-rc.5` integration or its retained `0.4.0-rc.4` rollback.

## Script creation in multi-edit

- One bounded `studio_multi_edit_v2` transaction may combine edits of existing
  exact script paths with creation of new `Script`, `LocalScript`, or
  `ModuleScript` instances beneath existing exact unambiguous parents.
- Every create names its exact parent, name, class, initial UTF-8 source, and
  mandatory `expected_absent: true` assertion. There is no overwrite mode.
- Host and Studio independently preflight every existing revision, create
  parent, absent full path, class, source, duplicate, and aggregate bound before
  the first write. Existing edits run in input order, followed by creates in
  input order.
- Prepare, apply, recovery, direct, and background-job receipts use a closed
  discriminated v2 edit/create target contract. They remain bound to exact
  `studio_id`, client identity, document epoch, generation, transaction,
  request, target ordering, and content revisions.
- Apply, create, restore, and transaction-owned destruction commit boundaries
  revalidate Edit/document state and the exact prepared path/Instance binding.
  Same-generation cached safe-terminal recovery is explicitly labeled and
  binds the prior terminal outcome and receipt SHA-256; uncertain or applied
  receipts are never replayed as safe.

## Deletion boundary and recovery

This release does not expose general deletion. Compensating rollback may
destroy only the retained Instance proven created by that exact transaction,
and only while its unique full path, exact class, name, source bytes, and
SHA-256 still match the prepared plan in the same connection generation and
it has zero children, zero attributes, and zero tags. Moved, renamed, edited,
decorated, replaced, ambiguous, or unavailable created content fails closed as
`recovery_required`; a pre-existing user instance is never deleted or
replaced.

Roblox Studio exposes no transaction spanning multiple script sources.
Accordingly, multi-edit remains `v2_partial`: all-target preflight, per-target
CAS, exact read-back, compensation, and explicit recovery are provided, but
cross-script atomicity is not claimed.

## Safety and release status

- Every operational call still requires explicit `studio_id`; discovery is the
  only exception.
- Same-session writes remain FIFO-serialized; different sessions remain
  isolated and may run concurrently.
- Reconnect generation fencing, uncertainty quarantine, job/direct result
  parity, terminal audit evidence, and the no-global/default/no-v1-fallback
  boundaries remain intact.
- Arbitrary Luau, general instance deletion, asset operations, and unrelated
  capability expansion remain out of scope.
- This candidate is experimental and capability parity remains incomplete. It
  must not be installed, live-mutated, tagged, or published until isolated
  qualification passes and a separate live gate is authorized.
