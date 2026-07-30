# Roblox Studio MCP Multisession 0.4.0-rc.7

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

Version `0.4.0-rc.7` is an isolated, unpublished correction to the rejected
rc.6 script-lifecycle candidate. Rc.6 proved native creation but failed its
live gate because a successful apply closed normal mutation recovery before
the host could admit later transaction-owned cleanup. Rc.6 remains immutable
and rejected; this candidate does not change the installed `0.4.0-rc.5`
integration or its retained `0.4.0-rc.4` rollback.

## Successful-creation cleanup

- A successful apply that creates one or more scripts now returns a separate
  cleanup authorization bound to the exact Studio, client, document epoch,
  connection generation, transaction, prepare receipt, apply request, apply
  receipt, and ten-minute retention contract.
- `studio_cleanup_multi_edit_v2` accepts only the original transaction UUID,
  apply-receipt SHA-256, and cleanup-authorization SHA-256. Callers cannot
  provide paths, classes, source, or delete targets.
- The Studio plugin retains one cleanup grant per session independently of
  normal recovery state. Bounded reads and an edit-only multi-edit may run
  while that grant is available, allowing an existing script in a mixed
  transaction to be restored before created scripts are removed.
- Direct and background-job cleanup use the same closed result schema,
  identity-bound receipts, same-session FIFO, cross-session isolation, and
  exact late-result resolution evidence.

## Deletion boundary

Before any deletion, Studio preflights every retained created target. A target
is eligible only when the retained Instance is still at the exact retained
parent/path/name with the exact class and source bytes/SHA-256 and still has
its original bounded mutable-property fingerprint, no post-exposure property
change, and zero children, attributes, and tags. A pre-existing, replaced,
moved, renamed, edited, decorated, duplicated, or unavailable instance is
preserved.

Cleanup has three explicit outcomes:

- `cleaned`: every transaction-created target is proven deleted or already
  absent.
- `refused`: preflight found drift; no new deletion was dispatched.
- `cleanup_required`: a dispatched cleanup could not be proven terminal. The
  session is quarantined. Only the same exact cleanup in the same generation
  may reconcile already-absent targets and remaining unchanged targets before
  the original deadline.

Safe terminal receipts are cached only for bounded response-loss replay.
Expiry never renews deletion authority: an unused grant retires, while
dispatched or partial cleanup becomes settlement-only quarantine that accepts
safe late evidence but cannot dispatch another deletion. Reconnect,
wrong-session reuse, wrong hashes, consumed authorization, and replay after
retirement fail before mutation. Broker lifecycle health reports a retained
cleanup authorization or uncertain cleanup as an explicit stop blocker, and
bounded audit tombstones retain only identity digests and terminal evidence.

## Safety and release status

- Every operational call still requires explicit `studio_id`; discovery is the
  only exception.
- There is no general delete, arbitrary Luau, active/default Studio route,
  silent v1 fallback, or cross-script atomicity claim.
- Capability parity remains incomplete and `multi_edit` remains `v2_partial`.
- A separate, explicitly authorized disposable-place live gate is still
  required before installation or publication of rc.7.
