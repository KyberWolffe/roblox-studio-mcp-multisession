# Roblox Studio MCP v2 0.4.0-dev.2

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This is an unreleased, development-only Phase 2 line. It advances the isolated
`0.4.0-dev.1` tree, state, and script-discovery milestone without changing the
immutable local `v0.3.0-rc.4` source checkpoint, restore bundle, release
identity, installed plugin, or rollback target.

This candidate is not installed and has not been published to GitHub. Local
artifacts may be staged only in isolated disposable homes. Any future live
installation requires the full release, security, reproducibility, rollback,
and explicit live-acceptance gates.

The instance-inspection slice adds:

- explicit-session `studio_inspect_instance_v2`, which accepts only a nonempty
  array of exact child-name segments and fails closed on duplicate sibling
  names;
- Edit-only, generation-fenced observation through a fixed reviewed
  34-selector property allowlist, with no arbitrary property reflection,
  script-source access, Luau evaluation, `UniqueId`, or security-identity
  exposure;
- closed, bounded value encoding for allowlisted properties and attributes,
  plus sorted tags, immediate children, and descendant class counts under
  explicit depth, scan, time, and output budgets;
- an exact identity-bearing output schema pinned independently from the input
  schema, with host-side validation required before direct delivery or job
  retention; authenticated results that violate selector-group, value-codec,
  child-boundary, or descendant-count invariants fail closed; and
- an honest `v2_partial` parity route. The official `inspect_instance` shape
  remains review-only and incompatible because it requests case-insensitive
  dot-path multi-match and all-readable-property behavior that this safe
  contract deliberately does not expose.

Every operational v2 tool still requires explicit `studio_id`. Discovery is
the only exception. Unknown or changed upstream schemas remain disabled, and
no missing capability silently falls back to v1.
