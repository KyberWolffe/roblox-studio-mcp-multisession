# Roblox Studio MCP Multisession

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

Roblox Studio MCP Multisession is a side-by-side local integration for safely
operating multiple Studio sessions from concurrent Codex tasks. Its short
display name is **Studio MCP Multisession**, and its canonical Codex server
name is `Roblox_Studio_Multisession`. Every Studio-bound tool requires an
explicit, server-assigned `studio_id`. There is no active Studio pointer,
implicit single-session choice, or default-session fallback.

Version `0.4.0-rc.7` is the current published experimental prerelease. It
corrects the rejected rc.6 script-lifecycle candidate, retains expected-absent
creation of bounded `Script`, `LocalScript`, and `ModuleScript` instances, and
adds a distinct, identity-bound authorization for later cleanup of only exact
unchanged transaction-created scripts. Transactional upgrades from
`0.4.0-rc.5` retain rc.5 as the immediate rollback; installed
`0.4.0-rc.4` remains an older recovery release.
The `0.3.0-rc.4` restore artifact remains available as older recovery history.
Rc.5 transactionally replaced an owned former
`Roblox_Studio_v2` Codex registration with
`Roblox_Studio_Multisession`; the two names must never remain active together.
The public `_v2` tool names, authenticated `/v2` routes, Python/internal
identifiers, support root, former launcher aliases, plugin path, archive
basename, and manifest format remain unchanged. Rc.5 also installs canonical
launcher/manager names and points the sole Codex registration at the canonical
launcher. The retained physical aliases form an intentional migration bridge
so the `0.4.0-rc.4` bootstrap, update journal, and byte-for-byte one-step
rollback remain usable. The current public experimental prerelease tag
[`v0.4.0-rc.7`](https://github.com/KyberWolffe/roblox-studio-mcp-multisession/releases/tag/v0.4.0-rc.7)
is pinned to qualified commit
`63dd793f385ebb9c992fd325185acae07c27aa21` and source tree
`f9f4921e779c4d46a4cef8bb1e3af3053337d947`. The tag and release artifacts
remain immutable; later default-branch documentation does not replace them.

> **Experimental prerelease:** the safe Multisession surface does not yet
> cover all 25 modern v1 capabilities. Twelve P0 rows remain partial or
> deferred. Publication is allowed only under a prerelease tag, and missing
> operations never fall back to a global v1 Studio selector. See the exact
> [capability parity matrix](docs/CAPABILITY_PARITY.md).

## Supported system

- Native Apple Silicon Mac (`arm64`)
- macOS
- Python 3.9 or newer
- Roblox Studio
- Codex desktop or CLI

Intel Macs and processes running through Rosetta are intentionally unsupported.
The bootstrap and installer check the platform before creating or changing any
files.

## What Multisession changes

```text
Codex task A ─ stdio frontend ─┐
Codex task B ─ stdio frontend ─┼─ local authenticated broker
Codex task N ─ stdio frontend ─┘       │
                                       ├─ studio_id A ─ Studio A plugin
                                       ├─ studio_id B ─ Studio B plugin
                                       └─ studio_id N ─ Studio N plugin
```

Each Studio owns its operation queue, lifecycle state, console, jobs,
correlation records, reconnect generation, and quarantine state. Work for
different IDs can overlap. Conflicting work for one ID serializes.

The design is dynamic; it is not limited to two sessions. Practical capacity
depends on local Studio processes, memory, CPU, and loopback traffic.

## Safety boundary

- `studio_id` selects a route; it is not authorization. Client and Studio
  credentials, capability policy, document identity, and reconnect generation
  are checked independently.
- The broker accepts only loopback traffic and rejects browser-origin requests.
- Credentials are generated during installation and never shipped in source or
  release artifacts.
- The plugin exposes a closed operation surface. It does not provide host
  shell execution, arbitrary filesystem access, arbitrary Luau evaluation,
  caller-selected URLs, native input injection, or a global Studio selector.
- Play/Stop uses transition nonces, one-time bridge credentials, explicit
  two-phase acceptance and observation, replay fences, bounded watchdogs, and
  observed return to Edit mode.
- New upstream tool shapes fail closed until an adapter, Studio handler, and
  isolation tests are deliberately added.

See [SECURITY.md](SECURITY.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Normal use

Users name the Studio place or project they want Codex to operate:

> Update the lighting in My Test Place, then playtest it.

Codex discovers registered windows with `list_roblox_studios_v2`, resolves the
named place to its current `studio_id`, and includes that ID on every tool call.
The ID, broker, locks, and reconnect generations are internal routing details;
normal users do not select a global Studio or copy IDs by hand.

If two open windows have the same name, or an unsaved document has no stable
name, Codex must use the discovery metadata to disambiguate and ask the user
only when a real ambiguity remains. It must never guess or route through an
active/default Studio.

V1 remains available only as an explicit fallback for an operation that v2
does not support. V2 never silently delegates to it. V1's active-Studio model
is not safe for multiplexing concurrent tasks, so concurrent work must stop or
be isolated before an intentional v1 fallback.

## Build locally

The repository has no third-party Python dependency:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -v
python3 -B scripts/release_dry_run.py
```

The release dry run audits the repository, builds the deterministic archive
twice, compares it byte-for-byte, audits the archive, and exercises the
installer in a temporary home. It does not touch real Codex, Roblox Studio, or
installed v1 or Multisession files.

Before a rendered candidate may be used for a live Studio gate, its exact
hashed `.rbxmx` source must also pass
`scripts/native_studio_compile_smoke.py`. The hard native identity gate binds
the exact Studio main-executable hash, Info.plist version/build and bundle
executable, and narrow Apple/Roblox signing identity with the executable's
CDHash. Full-bundle `codesign --verify --deep --strict` remains useful
diagnostic/provenance evidence, but is not a hard functional prerequisite.
Compilation evidence covers the exact sole `Main` source. The exact rc.7
rendered plugin also passed the linked load/registration gate, clean-log
checks, bounded explicit-session reads, and concurrent cross-session isolation
before publication. Future candidates must repeat the applicable linked gate.
See `docs/TESTING.md`.

To build only the portable release:

```bash
python3 -B scripts/build_durable_release.py
```

## Install from a GitHub Release

Do not install from a mutable `main` branch. Use an exact version tag and the
published SHA-256.

The bootstrap and direct `install.py install` path are for a fresh install or
the same version only. If any different Multisession version is already
installed, use its installed manager. The rc.4-to-rc.5 migration necessarily
begins through the former `roblox-studio-mcp-v2-manage update` path; after
rc.5 activates, use `roblox-studio-mcp-multisession-manage`. Direct
cross-version replacement is refused unless it is the exact candidate inside
that live, nonce-fenced update transaction.

### Published `0.4.0-rc.7` prerelease

The exact public release files are:

| File | SHA-256 |
|---|---|
| `roblox-studio-mcp-v2-0.4.0-rc.7-macos-arm64.tar.gz` | `2f116b0a072c59513e3a0f63857c2f0559a17ead5a8d06bb9d525ac00d59d7b8` |
| `roblox-studio-mcp-v2-0.4.0-rc.7-macos-arm64.tar.gz.sha256` | `dfce91f45171396820d49d22a2c5f55b7ccef356bd66069bc7879e2c5612e362` |
| `roblox-studio-mcp-v2-bootstrap-0.4.0-rc.7.py` | `e4f35d878024a3c73d6276bc512236e1cad8637c98894da976b233d556cd346b` |
| `roblox-studio-mcp-v2-bootstrap-0.4.0-rc.7.py.sha256` | `17a3638a5b972532a520a02933c660971ec9799b26b4c369948b39c13df5236e` |
| `SHA256SUMS` | `ab023fbab1198c704006287024705cb57a599b478a235c767d398fa9a86ca14e` |

For a fresh install or reinstall of the same version, this command downloads
the exact bootstrap, verifies its fixed digest, and gives it the exact archive
digest:

```bash
/bin/bash -ceu 'd="$(mktemp -d)"; f="$d/roblox-studio-mcp-v2-bootstrap-${3#v}.py"; curl --fail --location --output "$f" "https://github.com/$1/$2/releases/download/$3/${f##*/}"; printf "%s  %s\n" "$4" "$f" | shasum -a 256 --check; python3 "$f" --owner "$1" --repo "$2" --tag "$3" --expected-sha256 "$5"; rm -- "$f"; rmdir -- "$d"' -- KyberWolffe roblox-studio-mcp-multisession v0.4.0-rc.7 e4f35d878024a3c73d6276bc512236e1cad8637c98894da976b233d556cd346b 2f116b0a072c59513e3a0f63857c2f0559a17ead5a8d06bb9d525ac00d59d7b8
```

The command deliberately does not pipe network content into a shell. The
verified bootstrap verifies the archive checksum and internal manifest before
invoking the fresh/same-version installer.

### Historical provenance

The following `0.3.0-rc.3` command is retained verbatim as provenance. It
downloads from an exact tag, verifies both fixed release digests, then runs
the verified bootstrap. The owner, repository, tag, and digests are pinned to
that release:

```bash
/bin/bash -ceu 'd="$(mktemp -d)"; f="$d/roblox-studio-mcp-v2-bootstrap-${3#v}.py"; curl --fail --location --output "$f" "https://github.com/$1/$2/releases/download/$3/${f##*/}"; printf "%s  %s\n" "$4" "$f" | shasum -a 256 --check; python3 "$f" --owner "$1" --repo "$2" --tag "$3" --expected-sha256 "$5"; rm -- "$f"; rmdir -- "$d"' -- KyberWolffe roblox-studio-mcp-multisession v0.3.0-rc.3 96d602fff3acb610dda09e1c0769c7864707267ac018004b9b6aa4c6f6f7a750 75a6f94f16e738c515eac3d9cc59a2f1ce3e645edacfe9783f06ac213ce72723
```

The matching historical explicit archive path is:

```bash
curl --fail --location --remote-name \
  "https://github.com/KyberWolffe/roblox-studio-mcp-multisession/releases/download/v0.3.0-rc.3/roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz"
curl --fail --location --remote-name \
  "https://github.com/KyberWolffe/roblox-studio-mcp-multisession/releases/download/v0.3.0-rc.3/roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256"
shasum -a 256 --check \
  roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256
tar -xzf roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz
python3 roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64/install.py install
```

The GitHub release assets use the same deterministic archive format as local
builds under `dist/`.

## Installed layout

The installer derives the user home safely and owns only:

- `~/Library/Application Support/RobloxStudioMCPv2`
- `~/Documents/Roblox/Plugins/StudioMCPv2SideBySide.rbxmx`
- `[mcp_servers.Roblox_Studio_Multisession]` in `~/.codex/config.toml`

It does not replace the existing `Roblox_Studio` MCP table or v1 plugin. Codex
starts the canonical Multisession launcher on demand; the launcher owns
predictable broker startup, shutdown, diagnostics, and logs.

The canonical table follows Codex's supported user MCP configuration contract.
See [`config/codex-multisession.example.toml`](config/codex-multisession.example.toml)
and the [official Codex MCP setup
guide](https://learn.chatgpt.com/docs/extend/mcp). The retained
[`config/codex-v2.example.toml`](config/codex-v2.example.toml) is a disabled
former-name migration reference, not a second active registration.

After installation:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" doctor
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" status
```

Restart Codex so it refreshes the `Roblox_Studio_Multisession` MCP tool cache.
Restart Studio or reload local plugins for already-open windows. Each place
must allow HTTP requests so its plugin can reach the fixed `127.0.0.1` broker;
when disabled, that Studio simply remains unregistered. After those gates,
name the desired place in the task; do not manually select a broker session.

## Repair, update, rollback, and uninstall

To update an installed rc.5 to rc.7 while retaining rc.5 as the immediate
rollback:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" \
  update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag v0.4.0-rc.7 \
  --expected-sha256 2f116b0a072c59513e3a0f63857c2f0559a17ead5a8d06bb9d525ac00d59d7b8
```

The exact one-step rollback is:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" \
  rollback \
  --to-version 0.4.0-rc.5 \
  --accept-current-version 0.4.0-rc.7
```

Historical rc.4-to-rc.5 migration remains supported:

To migrate an installed `0.4.0-rc.4` release to rc.5, first close Studio
windows and use rc.4's former manager name. The guarded update retains rc.4 as
the immediate rollback:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" \
  update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag v0.4.0-rc.5 \
  --expected-sha256 d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d
```

After rc.5 activates, use the canonical manager for status, repair, later
exact-tag updates, rollback, and uninstall:

```bash
MANAGER="$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage"
"$MANAGER" repair
"$MANAGER" update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag vNEXT \
  --expected-sha256 ARCHIVE_SHA256_FROM_TARGET_RELEASE
"$MANAGER" rollback --to-version PREVIOUS_VERSION --accept-current-version CURRENT_VERSION
"$MANAGER" uninstall
```

An update downloads only the selected tag, verifies its published checksum and
internal manifest, stages and validates it, retains the previous version, and
switches only after checks pass. A failed update leaves the installed version
and v1 fallback intact.

If the manager process or operating system interrupts the switch,
`status`/`doctor` expose the durable transaction record and plain `repair`
restores the exact pre-switch snapshot. Recovery verifies the marker,
snapshot, and retained lifecycle code; stops Multisession before restore; runs
the restored version's real doctor; and clears the marker only after success.
It never resumes a half-installed candidate and fails closed on tampering or a
stop refusal.

Update and rollback both use this journal. The journal identifies the
transition kind, and an interrupted rollback aborts to its pre-rollback
version. Atomic per-file `bin` restoration keeps the stable manager path
present across every tested interruption point.

The stable launcher is pinned to the Python used during installation. If that
interpreter moves after a Python or package-manager update, run the release
installer again or run `repair` with a current Python 3.9+; repair repins the
launcher without replacing machine credentials.

For a private repository, authenticate with GitHub separately, download the
archive and checksum, then use the same historical pinned example shape:

```bash
"$MANAGER" update \
  --tag v0.3.0-rc.3 \
  --archive /path/to/release.tar.gz \
  --checksum-file /path/to/release.tar.gz.sha256 \
  --expected-sha256 75a6f94f16e738c515eac3d9cc59a2f1ce3e645edacfe9783f06ac213ce72723
```

Multisession never asks for or stores GitHub credentials. See
[docs/GITHUB_DISTRIBUTION.md](docs/GITHUB_DISTRIBUTION.md) and
[release_tools/PORTABLE_INSTALL.md](release_tools/PORTABLE_INSTALL.md).

## Updating the Roblox tool catalog

Catalog versions are independent of the Multisession application version. The
manager can compare the installed catalog with a trusted local v1 cache or an
explicit local catalog artifact. Import requires review and exact digest
acceptance.

```bash
"$MANAGER" catalog diff --artifact /path/to/candidate-catalog.json
"$MANAGER" catalog import \
  --artifact /path/to/candidate-catalog.json \
  --accept-sha256 REVIEWED_SHA256
```

Compatible schema-only additions can regenerate the fixed handler catalog.
New, removed, renamed, or changed operation shapes stay quarantined until code
and tests support them. Replacement is atomic and rollback receipts are
hash-fenced.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Protocol](docs/PROTOCOL.md)
- [Studio plugin and catalog workflow](docs/DURABLE_PLUGIN.md)
- [Exact 25-tool capability parity](docs/CAPABILITY_PARITY.md)
- [Testing and release proof](docs/TESTING.md)
- [GitHub distribution](docs/GITHUB_DISTRIBUTION.md)
- [Design provenance and source limits](docs/SOURCE_AUDIT.md)
- [Published rc.7 release notes](docs/RELEASE_NOTES_0.4.0-rc.7.md)
- [Prior rc.5 release notes](docs/RELEASE_NOTES_0.4.0-rc.5.md)
- [Changelog](CHANGELOG.md)
- [License status](LICENSE_STATUS.md)
