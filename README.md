# Roblox Studio MCP v2

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

Roblox Studio MCP v2 is a side-by-side local integration for safely operating
multiple Studio sessions from concurrent Codex tasks. Every Studio-bound tool
requires an explicit, server-assigned `studio_id`. There is no active Studio
pointer, implicit single-session choice, or default-session fallback.

Version `0.3.0-rc.1` is a GitHub-distribution release candidate based on the
live-validated v2 `0.2.0` broker and Studio plugin.

> **Experimental prerelease:** the safe v2 surface does not yet cover all 25
> modern v1 capabilities. Twelve P0 rows remain partial or deferred. Publication
> is allowed only under a prerelease tag, and missing operations never fall back
> to a global v1 Studio selector. See the exact
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

## What v2 changes

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
  acknowledgements, replay fences, bounded watchdogs, and observed return to
  Edit mode.
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
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
python3 scripts/release_dry_run.py
```

The release dry run audits the repository, builds the deterministic archive
twice, compares it byte-for-byte, audits the archive, and exercises the
installer in a temporary home. It does not touch real Codex, Roblox Studio, or
installed v1/v2 files.

To build only the portable release:

```bash
python3 -B scripts/build_durable_release.py
```

## Install from a GitHub Release

Do not install from a mutable `main` branch. Use an exact version tag and the
published SHA-256.

The bootstrap and direct `install.py install` path are for a fresh install or
the same version only. If any different v2 version is already installed, use
the installed `roblox-studio-mcp-v2-manage update` command shown below. Direct
cross-version replacement is refused unless it is the exact candidate inside
that live, nonce-fenced update transaction.

The release publishes a small versioned bootstrap separately. The following is
one command: it downloads from an exact tag, verifies both fixed published
digests, then runs the verified bootstrap. Replace the owner and two digests:

```bash
/bin/bash -ceu 'd="$(mktemp -d)"; f="$d/roblox-studio-mcp-v2-bootstrap-${3#v}.py"; curl --fail --location --output "$f" "https://github.com/$1/$2/releases/download/$3/${f##*/}"; printf "%s  %s\n" "$4" "$f" | shasum -a 256 --check; python3 "$f" --owner "$1" --repo "$2" --tag "$3" --expected-sha256 "$5"; rm -- "$f"; rmdir -- "$d"' -- OWNER roblox-studio-mcp-v2 v0.3.0-rc.1 PUBLISHED_BOOTSTRAP_SHA256 PUBLISHED_ARCHIVE_SHA256
```

The command deliberately does not pipe network content into a shell. The
verified bootstrap then verifies the archive checksum and internal manifest
before invoking the fresh/same-version installer.

The explicit archive path is:

```bash
curl --fail --location --remote-name \
  "https://github.com/OWNER/roblox-studio-mcp-v2/releases/download/v0.3.0-rc.1/roblox-studio-mcp-v2-0.3.0-rc.1-macos-arm64.tar.gz"
curl --fail --location --remote-name \
  "https://github.com/OWNER/roblox-studio-mcp-v2/releases/download/v0.3.0-rc.1/roblox-studio-mcp-v2-0.3.0-rc.1-macos-arm64.tar.gz.sha256"
shasum -a 256 --check \
  roblox-studio-mcp-v2-0.3.0-rc.1-macos-arm64.tar.gz.sha256
tar -xzf roblox-studio-mcp-v2-0.3.0-rc.1-macos-arm64.tar.gz
python3 roblox-studio-mcp-v2-0.3.0-rc.1-macos-arm64/install.py install
```

GitHub release assets are not available until a repository owner publishes the
candidate. Local builds use `dist/` and the same archive format.

## Installed layout

The installer derives the user home safely and owns only:

- `~/Library/Application Support/RobloxStudioMCPv2`
- `~/Documents/Roblox/Plugins/StudioMCPv2SideBySide.rbxmx`
- `[mcp_servers.Roblox_Studio_v2]` in `~/.codex/config.toml`

It does not replace the existing `Roblox_Studio` MCP table or v1 plugin. Codex
starts the v2 launcher on demand; the launcher owns predictable broker startup,
shutdown, diagnostics, and logs.

The side-by-side table follows Codex's supported user MCP configuration
contract. See the [official Codex MCP setup
guide](https://learn.chatgpt.com/docs/extend/mcp).

After installation:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" doctor
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" status
```

Restart Codex so it refreshes the MCP tool cache. Restart Studio or reload
local plugins for already-open windows. Each place must allow HTTP requests so
its plugin can reach the fixed `127.0.0.1` broker; when disabled, that Studio
simply remains unregistered. After those gates, name the desired place in the
task; do not manually select a broker session.

## Repair, update, rollback, and uninstall

```bash
MANAGER="$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage"
"$MANAGER" repair
"$MANAGER" update \
  --owner OWNER \
  --repo roblox-studio-mcp-v2 \
  --tag v0.3.0-rc.1 \
  --expected-sha256 PUBLISHED_ARCHIVE_SHA256
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
snapshot, and retained lifecycle code; stops v2 before restore; runs the
restored version's real doctor; and clears the marker only after success. It
never resumes a half-installed candidate and fails closed on tampering or a
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
archive and checksum, then use:

```bash
"$MANAGER" update \
  --tag v0.3.0-rc.1 \
  --archive /path/to/release.tar.gz \
  --checksum-file /path/to/release.tar.gz.sha256 \
  --expected-sha256 PUBLISHED_ARCHIVE_SHA256
```

V2 never asks for or stores GitHub credentials. See
[docs/GITHUB_DISTRIBUTION.md](docs/GITHUB_DISTRIBUTION.md) and
[release_tools/PORTABLE_INSTALL.md](release_tools/PORTABLE_INSTALL.md).

## Updating the Roblox tool catalog

Catalog versions are independent of the v2 application version. The manager
can compare the installed catalog with a trusted local v1 cache or an explicit
local catalog artifact. Import requires review and exact digest acceptance.

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
- [Release notes](docs/RELEASE_NOTES_0.3.0-rc.1.md)
- [Changelog](CHANGELOG.md)
- [License status](LICENSE_STATUS.md)
