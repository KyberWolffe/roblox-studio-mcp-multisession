# Roblox Studio MCP v2 0.3.0-rc.4

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This local release candidate fixes the Workshop Play failure found during the
rc.3 two-place acceptance gate. It preserves explicit per-Studio routing,
two-phase Start/Stop observation, recovery fencing, and the installed v1
side-by-side fallback without using v1 as an active or default target.

This remains an experimental prerelease, not a claim that all 25 modern v1
tools are implemented. The formal matrix records 12 P0 gaps as partial or
deferred. Missing capabilities fail closed.

## Highlights

- The fixed bridge runs as Studio plugin code in the PlayServer DataModel.
  Loopback access no longer depends on the place's saved
  `HttpService.HttpEnabled` setting.
- The temporary server Script is disabled and acts only as a correlated,
  transition-owned bootstrap marker. Cleanup remains exact and unsaved.
- One-shot bridge tokens, transition nonces, document epochs, place/game
  identity, activation TTLs, watchdog acknowledgements, and explicit
  `studio_id` routing are unchanged.
- Pre-broker bridge failures return a bounded structured failure code in the
  correlated runner receipt.
- Positively terminal disconnected sessions are safe during a reconnect grace
  period, then compacted into bounded non-secret audit tombstones. Any pending
  request, uncertain outcome, nonterminal job, non-Edit observation, or active
  Play transition prevents retirement.
- V1 remains installed side by side and is never selected through a global or
  silent fallback.

## Local candidate assets

- `roblox-studio-mcp-v2-0.3.0-rc.4-macos-arm64.tar.gz`
- `roblox-studio-mcp-v2-0.3.0-rc.4-macos-arm64.tar.gz.sha256`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.4.py`
- `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.4.py.sha256`
- `SHA256SUMS`

The final local digests must match the reproducible build output. This
candidate is not authorized for GitHub publication.

## Upgrade notes

Install only the locally built, audited rc.4 archive through the transactional
manager update path. Retain rc.3 as the one-step rollback target. Open Studio
windows must reload the installed plugin before the live gate; if that requires
an interactive reload or reopen, stop and request that precise human action.

## Evidence boundary

Repository tests, release audits, deterministic builds, and the isolated
install/update/rollback proof do not open Studio or run Play. The exact
`Experiments` plus `Workshop` live gate is separate and must use only explicit
Roblox Studio MCP v2 session IDs.

## Known limitations

- Intel Macs and Rosetta are unsupported and rejected before installation
  mutation.
- Session and job state is in memory; after a clean broker restart, callers
  rediscover fresh `studio_id` values.
- One local MCP principal is installed per user; `studio_id` is routing
  context, not a multi-user authorization identity.
- Screenshot and Scriptable `InputBinding` operations remain subject to Roblox
  Studio permissions and platform support.
