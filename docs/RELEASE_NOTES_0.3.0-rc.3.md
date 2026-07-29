# Roblox Studio MCP v2 0.3.0-rc.3

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This release candidate fixes slow, concurrent Play transitions while
preserving explicit per-Studio routing. It targets native Apple Silicon macOS
only.

This remains an experimental prerelease, not a claim that all 25 modern v1
tools are implemented. The formal matrix records 12 P0 gaps as partial or
deferred. Missing capabilities fail closed and never route through a global v1
active Studio. See [V1 capability parity](CAPABILITY_PARITY.md).

## Highlights

- Start returns a correlated `starting` acceptance receipt after scheduling
  the exact transition. Server readiness is observed through read-only state.
- Stop binds one correlated command and returns `stopping`; terminal Edit is
  reported only after the runner returns and Edit is positively observed.
- Slow Studio startup has bounded 180-second pre-attach, 150-second
  activation, 180-second server-watchdog, and refreshed 210-second active
  lifetimes.
- Disconnected controllers retain a non-secret broker recovery view, avoiding
  mutation replay or guessed state.
- Discovery and direct state reads consistently expose `starting`, `play`,
  `stopping`, and terminal Edit.
- Published asset names are used for exact multi-window place identity when
  Roblox supplies them.
- V1 remains installed but is never used as a global or silent fallback.

## Release assets

- `roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz`
- `roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py.sha256`
- `SHA256SUMS`

The final published digests must match the locally reproduced `SHA256SUMS`.
Release archives contain source and standard-library Python/Luau, not machine
credentials or native executables.

## Upgrade notes

The installed manager accepts a reviewed local artifact with the exact rc.3
version and digest:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" \
  update \
  --tag v0.3.0-rc.3 \
  --archive ./roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz \
  --checksum-file ./roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256 \
  --expected-sha256 75a6f94f16e738c515eac3d9cc59a2f1ce3e645edacfe9783f06ac213ce72723
```

The update retains rc.2 as the one-step rollback target. Restart Codex after
the version switch so it reloads MCP schemas. Reload local Studio plugins or
restart open Studio windows before using the new plugin.

## Evidence boundary

Repository tests, release audits, deterministic builds, and the isolated
install/update/rollback proof do not open Studio or run Play. Live acceptance
against named places is intentionally a separate operator-controlled step.

## Known limitations

- Intel Macs and Rosetta are unsupported and rejected before installation
  mutation.
- Session and job state is in memory; after a clean broker restart, callers
  rediscover fresh `studio_id` values.
- One local MCP principal is installed per user; `studio_id` is routing
  context, not a multi-user authorization identity.
- Studio HTTP requests must be enabled for the local plugin to reach the fixed
  loopback broker.
- Screenshot and Scriptable `InputBinding` operations remain subject to Roblox
  Studio permissions and platform support.
