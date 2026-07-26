# Roblox Studio MCP v2 0.3.0-rc.2

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This release candidate packages the validated v2 0.2.0 architecture for
repeatable installation and update from a versioned GitHub Release. It targets
native Apple Silicon macOS only.

This is an experimental prerelease, not a claim that all 25 modern v1 tools are
implemented. The formal matrix records 12 P0 gaps as partial or deferred.
Missing capabilities fail closed and never route through a global v1 active
Studio. See [V1 capability parity](CAPABILITY_PARITY.md).

## Highlights

- Authenticated Python loopback clients now ignore all ambient HTTP proxy
  settings and refuse redirects, so local bearer credentials cannot be routed
  through a proxy or forwarded away from the exact broker endpoint.
- GitHub CI suppresses Python bytecode for every project command and audits the
  source tree before and after tests.
- Multiple Studio windows register as distinct authenticated sessions.
- Every operational call names an exact `studio_id`; there is no global active
  Studio selection or one-Studio fallback.
- Work for different sessions can run concurrently. Conflicting writes and
  Play/Stop transitions within one session serialize.
- The side-by-side Studio plugin uses documented plugin-security lifecycle
  APIs and proves return to Edit mode before reporting Stop complete.
- Users name a place or project normally. Codex resolves it through v2
  discovery and handles `studio_id` internally, asking only when duplicate or
  unsaved windows are genuinely ambiguous.
- The installer preserves the existing `Roblox_Studio` MCP entry and plugin as
  the v1 fallback.
- Credentials are generated per machine and are excluded from source and
  release archives.
- Updates are pinned, checksum-verified, staged, validated, and recoverable.
- Upstream Roblox tool-catalog changes are diffed and review-gated; unknown
  operation shapes remain quarantined.

## Release assets

- `roblox-studio-mcp-v2-0.3.0-rc.2-macos-arm64.tar.gz`
- `roblox-studio-mcp-v2-0.3.0-rc.2-macos-arm64.tar.gz.sha256`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.2.py`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.2.py.sha256`
- `SHA256SUMS`

The final published digests must match the locally reproduced
`SHA256SUMS`. Release archives contain source and standard-library Python/Luau,
not machine credentials or native executables.

## Upgrade notes

The installed manager accepts a selected version tag:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" \
  update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag v0.3.0-rc.2 \
  --expected-sha256 8ba8797cda606c0e5f54cc5447ced92a7ef2a7bc4f3d91d180bc03a363e49d57
```

For a private repository, download the archive and checksum through an
authenticated GitHub client, then use the manager's offline `--archive`,
`--checksum-file`, and `--expected-sha256` options. Tokens are never passed to
or stored by v2.

Restart Codex after an install or version switch so it reloads MCP schemas.
Reload local Studio plugins or restart open Studio windows after plugin
changes. Enable **Allow HTTP Requests** for each place that should register
with the loopback broker. A disabled place stays safely unregistered.

V1 remains installed for deliberate use when v2 lacks an operation, but v2
never silently falls back. Do not use v1's selection-based routing for
concurrent Studio tasks.

The installed launcher records the Python interpreter used at install time. If
that interpreter is moved by a Python or package-manager update, rerun repair
with a current Python 3.9+ to repin it.

## Evidence boundary

The 0.2.0 broker/plugin operation surface and lifecycle behavior were validated
with two explicitly authorized disposable Studio places before this repository
was prepared. The 0.3.0-rc.2 work changes packaging, architecture enforcement,
proxy isolation, update, audit, and CI behavior. Its release proof runs in an
isolated temporary home and does not open Studio or repeat live place edits.

## Known limitations

- Intel Macs and Rosetta are unsupported and rejected before installation
  mutation.
- Session and job state is in memory; after a clean broker restart, callers
  rediscover fresh `studio_id` values.
- One local MCP principal is installed per user; `studio_id` is routing
  context, not a multi-user authorization identity.
- Studio HTTP requests must be enabled for the local plugin to reach the fixed
  loopback broker.
- Screenshot and Scriptable `InputBinding` operations remain subject to
  Roblox Studio permissions and platform support.
