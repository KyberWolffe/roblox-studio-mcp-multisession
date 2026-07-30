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

## Publication and qualification

This exact candidate is published as an experimental prerelease:

- tag: `v0.4.0-rc.5`;
- commit: `923422254e95050f0fe66bacc0114e9ace2789c5`;
- source tree: `3e3713045821412b6a6bbe0a4db9e27ab7bb58e3`;
- Apple-Silicon archive SHA-256:
  `d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d`;
- bootstrap SHA-256:
  `e4f35d878024a3c73d6276bc512236e1cad8637c98894da976b233d556cd346b`;
- `SHA256SUMS` SHA-256:
  `fa7339b2271f815e7e43bdd7a93008646bf062b701ddcf72f358c99b25924b4f`.

The exact rendered plugin compiled in Studio, loaded and registered under the
canonical Codex name, and passed bounded explicit-session state, search, grep,
inspection, same-session serialization, cross-session isolation, and
revision-protected multi-edit routing gates. All 479 automated tests passed.
The retained `0.4.0-rc.4` package remains the immediate guarded rollback for
systems migrated from rc.4.

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
remain marked as a prerelease. The qualification above applies only to the
exact tagged source and fixed release digests; it is not a full-parity claim.
