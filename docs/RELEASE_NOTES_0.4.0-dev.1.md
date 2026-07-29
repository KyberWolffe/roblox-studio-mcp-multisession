# Roblox Studio MCP v2 0.4.0-dev.1

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This is an unreleased, development-only Phase 2 line. It is based on the
immutable local `v0.3.0-rc.4` checkpoint and must not replace the installed
rc.4 integration during ordinary parity development.

The line incrementally reviews and implements bounded Studio tool parity. It
does not claim full parity, does not auto-enable newly discovered upstream
tools, and never introduces active/default Studio routing or a silent v1
fallback.

No release artifacts or GitHub publication are authorized for this development
version. Any future installable candidate requires the full release, security,
reproducibility, rollback, and explicit live-acceptance gates.

Current development slice:

- bounded deterministic tree filtering and cursor pagination, while the
  official `search_game_tree` schema remains review-only and parity remains
  partial;
- conservative Studio state reporting that exposes raw predicate read status
  and only the routable Edit controller, never treating the lifecycle-only
  PlayServer bridge as a general Server/Client channel; and
- host-side identity, generation, and closed-context validation before a
  connected durable state result can affect cached session mode.
