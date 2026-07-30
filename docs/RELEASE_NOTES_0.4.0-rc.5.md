# Roblox Studio MCP Multisession 0.4.0-rc.5

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

Version `0.4.0-rc.5` is a public-name migration candidate. It carries forward
the corrected rc.4 Phase 2 behavior while changing the user-facing product and
Codex registration to names that describe the actual multi-window design.
The durable installed `0.4.0-rc.4` integration and its immutable restore
bundle remain the required immediate rollback target. The
`0.3.0-rc.4` restore artifact remains older recovery history.

## Canonical names

| Surface | Canonical rc.5 value |
|---|---|
| Human product | `Roblox Studio MCP Multisession` |
| Display/short name | `Studio MCP Multisession` |
| Codex server | `Roblox_Studio_Multisession` |
| Repository/release slug | `roblox-studio-mcp-multisession` |

The installer migrates only an exactly owned former
`[mcp_servers.Roblox_Studio_v2]` table. It snapshots and hash-binds the complete
pre-migration Codex configuration, writes
`[mcp_servers.Roblox_Studio_Multisession]`, and verifies that exactly one of
the two registration names is active. An unowned, drifted, dual-active, or
otherwise ambiguous configuration fails closed for review.

Codex must be restarted after install, update, rollback, or repair changes the
registration. Studio windows must be closed before a plugin-file transaction
and reopened afterward; the user remains responsible for any Save or Don't
Save prompt.

## Legacy-filename bridge

Rc.5 intentionally does not rename compatibility-sensitive physical or
protocol identities:

- public tool names ending in `_v2` and authenticated `/v2` routes;
- Python package and internal wire identifiers;
- `~/Library/Application Support/RobloxStudioMCPv2`;
- `~/Documents/Roblox/Plugins/StudioMCPv2SideBySide.rbxmx`;
- canonical `roblox-studio-mcp-multisession` launcher/manager names are added,
  while the former `roblox-studio-mcp-v2` launcher and
  `roblox-studio-mcp-v2-manage` manager remain compatibility aliases;
- `roblox-studio-mcp-v2-*` archive/bootstrap filenames and existing manifest
  formats.

This is a deliberate bridge, not incomplete branding. The `0.4.0-rc.4`
bootstrap, archive manifests, transaction journal, retained package, and
rollback proof all bind exact legacy filenames. Preserving them allows the
normal guarded update path to activate rc.5 and allows one-step rollback to
restore `0.4.0-rc.4` byte-for-byte. Rc.4 artifacts, tags, hashes, commands,
and provenance records remain immutable.

## Functional and safety boundary

This candidate changes public naming and registration ownership only. It does
not add a global/default Studio selector, silently invoke v1, widen the
capability catalog, rename tools or routes, or weaken per-session credentials,
locks, correlation, generation fencing, uncertainty quarantine, or Play
two-phase semantics.

Every operational tool still requires an explicit `studio_id`; discovery is
the sole exception. Capability parity remains incomplete and the release must
remain marked as a prerelease. Source preparation alone is not an installation
or live-validation claim.
