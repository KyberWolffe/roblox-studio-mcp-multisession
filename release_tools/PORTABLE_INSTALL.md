# Roblox Studio MCP Multisession — install, update, and recovery

This package installs Roblox Studio MCP Multisession beside the existing v1
integration. Its canonical Codex server name is
`Roblox_Studio_Multisession`. It never replaces the v1 executable, plugin, or
Codex table.

## Requirements

- Native Apple Silicon (`arm64`) macOS; Intel and Rosetta are unsupported
- Python 3.9 or newer
- Roblox Studio and Codex

The bootstrap and every mutating manager command check the platform/runtime
before changing files and return a clear non-mutating error when unsupported.

## Rc.6 script-lifecycle candidate

Version `0.4.0-rc.6` adds bounded expected-absent creation of `Script`,
`LocalScript`, and `ModuleScript` to the revision-protected multi-edit
transaction. It does not expose general deletion: compensation may remove only
the exact same-generation Instance proven created by that transaction while
its full identity, class, source revision, zero children, zero attributes, and
zero tags remain unchanged. Install or update only an exact qualified archive;
an isolated source checkout is not an authorization to replace the durable
installed rc.5 integration.

## Rc.5 legacy-filename bridge

Version `0.4.0-rc.5` changes the public product and Codex registration name.
It installs canonical `roblox-studio-mcp-multisession` launcher/manager names
while retaining the `RobloxStudioMCPv2` support root,
`StudioMCPv2SideBySide.rbxmx` plugin, former `roblox-studio-mcp-v2` launcher
aliases, `roblox-studio-mcp-v2-*` artifact basenames, and existing manifest
identity. The immutable `0.4.0-rc.4` bootstrap, package manifests, update
journal, and rollback proof bind those exact retained physical names.

An rc.5 update replaces only the exactly owned former
`[mcp_servers.Roblox_Studio_v2]` table with
`[mcp_servers.Roblox_Studio_Multisession]`. It snapshots and hash-binds the
complete pre-migration Codex configuration and refuses unowned drift or a
dual-active result. Keeping the legacy filenames allows one guarded update to
rc.5 and a byte-for-byte one-step rollback to `0.4.0-rc.4`.

## Install one exact GitHub release

Never install from mutable `main` content or pipe a web response into a shell.
The pinned `0.3.0-rc.3` commands below are retained verbatim as historical
release provenance. For a public repository, use the versioned bootstrap from
an exact tag and the two hashes published with that GitHub Release. The
repository README contains the copy/paste one-command form. Its trust sequence
is:

1. download
   `roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py` from tag `v0.3.0-rc.3`;
2. compare its SHA-256 with the fixed value in the Release;
3. run the verified bootstrap with `--owner`, `--repo`, exact `--tag`, and the
   Release's archive digest as `--expected-sha256`;
4. let it download the exact arm64 archive plus checksum sidecar, verify both
   the external digest and every file in the internal manifest, safely extract,
   then invoke `install.py`.

The bootstrap/direct install path is only for a fresh install or the same
version. If a different Multisession version is already installed, use the
installed manager's exact-tag `update` command below. No command-line flag can
bypass the live transaction nonce and exact on-disk from/to version fence.

For a fully manual/offline installation, download and verify these exact
assets:

```text
roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py
roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py.sha256
roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz
roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256
SHA256SUMS
```

Then either extract the verified archive and run:

```bash
python3 roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64/install.py install
```

or use the verified bootstrap offline:

```bash
python3 ./roblox-studio-mcp-v2-bootstrap-0.3.0-rc.3.py \
  --tag v0.3.0-rc.3 \
  --archive ./roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz \
  --checksum-file ./roblox-studio-mcp-v2-0.3.0-rc.3-macos-arm64.tar.gz.sha256 \
  --expected-sha256 ARCHIVE_SHA256_FROM_RELEASE
```

Private-repository authentication stays outside Multisession. Use an already
authenticated GitHub CLI to download the same exact tag assets, verify them,
then use the offline command. Never paste a GitHub token into Codex chat,
installer arguments, or Multisession configuration.

## Installed layout and setup gates

The installer derives the current user's paths and owns only:

```text
$HOME/Library/Application Support/RobloxStudioMCPv2
$HOME/Documents/Roblox/Plugins/StudioMCPv2SideBySide.rbxmx
[mcp_servers.Roblox_Studio_Multisession] in $HOME/.codex/config.toml
```

It refuses to adopt an unowned Multisession Codex table, backs up the config
byte-for-byte before editing it, and leaves
`[mcp_servers.Roblox_Studio]` outside Multisession ownership. During the rc.5
migration it recognizes only its exactly owned former
`[mcp_servers.Roblox_Studio_v2]` table and removes that table as it activates
the canonical name. Machine credentials are generated during installation,
stored only in `config/secrets.json` with mode `0600`, and never shipped.

After installation:

1. restart Codex to load/refresh `Roblox_Studio_Multisession`;
2. reload local plugins or restart every already-open Studio window;
3. enable **Allow HTTP Requests** in each place that should connect.

That Studio setting enables the fixed local plugin-to-broker HTTP bridge; it
does not grant arbitrary local execution. A window with HTTP disabled simply
does not register.

In normal tasks, users name the desired place/project. Codex discovers the
Multisession sessions and supplies the matching internal `studio_id`; users do
not copy IDs, select a global active Studio, manage locks, or start a broker
manually. Codex asks only when duplicate names or unsaved documents are
genuinely ambiguous. Every operational call remains explicitly
session-targeted internally.

## Status and repair

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" doctor
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" status
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" repair
```

Repair is idempotent. It reconstructs damaged Multisession-owned release,
launcher, catalog, or plugin files from the installed package. It will not
overwrite a modified Codex registration without `--replace-owned-config`, and
it never edits v1. Missing/damaged credentials fail closed; deliberate
`repair --rotate-secrets` generates a new local pair and matching plugin.

Repair is also the one-command recovery path if the manager process or
operating system interrupts an update or rollback. `status` and `doctor`
report the transition kind, interrupted target, pre-switch version, snapshot
digest, and whether recovery is safe. The durable pending marker lives outside
the snapshotted state, so a second `repair` can resume if recovery itself is
interrupted. Recovery:

1. locks release management;
2. revalidates the marker and every pre-switch snapshot byte;
3. uses trusted current code to verify the retained package's exact manifested
   file set before importing any retained installer;
4. requires the release runtime tree to equal the exact manifested payload
   before running retained lifecycle code;
5. requires an explicit acknowledgement that Multisession stopped;
6. restores the exact pre-switch Multisession-owned bytes;
7. runs the restored version's real doctor;
8. clears the pending marker last.

It never continues or retries a half-installed candidate. A forged marker,
out-of-root snapshot, changed snapshot/retained package, unsafe path, or stop
refusal fails closed before restore. The marker remains for diagnosis and a
later safe retry; v1 remains untouched.

The `bin` tree is restored file-by-file with atomic replacement: its stable
manager path is never removed as a directory-swap intermediate. Tests inject
an interruption immediately before and after every launcher/manager
replacement and complete the next `repair` from a fresh process.

## Exact tagged update and release rollback

There is no automatic release update. The one rc.4-to-rc.5 migration is
started with rc.4's retained `roblox-studio-mcp-v2-manage`; rc.5 then installs
the canonical manager used by the examples below. For a later public
exact-tag update:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag vNEXT_VERSION \
  --expected-sha256 ARCHIVE_SHA256_FROM_RELEASE
```

For a reviewed local/private artifact:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" update \
  --tag vNEXT_VERSION \
  --archive ./roblox-studio-mcp-v2-NEXT_VERSION-macos-arm64.tar.gz \
  --checksum-file ./roblox-studio-mcp-v2-NEXT_VERSION-macos-arm64.tar.gz.sha256 \
  --expected-sha256 ARCHIVE_SHA256_FROM_RELEASE
```

The manager checks the exact tag, filename, external checksum, and
hash-verified file manifest before loading candidate code. It stages the
versioned package,
retains the prior package, serializes the switch, and snapshots only the shared
Multisession-owned bytes plus exact Codex config/plugin bytes. A durable
transaction marker outside that snapshot makes recovery resumable after a
manager-process or operating-system interruption.
The candidate must pass installed-path doctor checks. Any failure restores the
snapshot instead of activating unverified or partially installed bytes.

Doctor/status reports retained versions and the latest one-step rollback
target. Rollback is receipt-fenced and requires the observed current version:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" rollback \
  --to-version PREVIOUS_VERSION \
  --accept-current-version CURRENT_VERSION
```

Restart Codex and reload/restart open Studio windows after update or rollback.
Rollback uses the same durable pre-switch marker as update. An interrupted
rollback is aborted back to the exact version that was active before rollback;
recovery never guesses that the partially restored target should be continued.

## Review-gated Roblox tool catalog updates

The Roblox tool catalog version is independent from the Multisession release
version. Review a trusted local candidate first:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" catalog diff --artifact ./candidate-catalog.json
```

Only compatible schemas mapped to tested durable handlers can be imported,
and the displayed checksum must be accepted explicitly:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" catalog import \
  --artifact ./candidate-catalog.json \
  --accept-sha256 REVIEWED_SHA256
```

New operation shapes remain quarantined until an adapter and contract tests
exist. Catalog replacement is transactional and its rollback receipts are
hash-fenced; no tool ever gains a global/default-Studio operational fallback.

## Uninstall and v1 fallback

Return every Multisession Studio session to Edit mode, then run:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" uninstall
```

Uninstall stops only Multisession, removes only its exactly owned canonical
Codex block and legacy-named plugin, and moves the support root to a
timestamped recoverable sibling rather than deleting it. The original Codex
bytes are restored exactly and v1 remains untouched. A mode-0600 coordination
lock remains in the support root's parent so uninstall and a concurrent
reinstall cannot select different lock-file inodes; it contains no credentials
or user data.

V1 may be used only as an explicit fallback for an operation v2 does not yet
support. Codex must never silently reroute a v2 request through v1: v1's
active-Studio selection model is unsafe for concurrent multi-window use.
