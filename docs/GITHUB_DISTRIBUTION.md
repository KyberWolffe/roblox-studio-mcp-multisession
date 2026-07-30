# Roblox Studio MCP Multisession GitHub distribution

This document describes the release flow for
`KyberWolffe/roblox-studio-mcp-multisession`. Never substitute a mutable branch
for an exact version tag.

Version `0.4.0-rc.5` uses the canonical product name Roblox Studio MCP
Multisession and Codex server name `Roblox_Studio_Multisession`. Its archive,
bootstrap, plugin, support-root, manifest, and former launcher-alias names
intentionally retain their `v2` physical identities. The immutable
`0.4.0-rc.4` update and rollback machinery verifies those exact names, so
renaming the compatibility artifacts would break the migration bridge.

## Public repository

A public release requires no GitHub credentials to install. This current rc.5
command downloads the versioned bootstrap from the exact immutable tag,
verifies its published digest, pins the archive digest, and runs only the
verified file:

```bash
/bin/bash -ceu 'd="$(mktemp -d)"; f="$d/roblox-studio-mcp-v2-bootstrap-${3#v}.py"; curl --fail --location --output "$f" "https://github.com/$1/$2/releases/download/$3/${f##*/}"; printf "%s  %s\n" "$4" "$f" | shasum -a 256 --check; python3 "$f" --owner "$1" --repo "$2" --tag "$3" --expected-sha256 "$5"; rm -- "$f"; rmdir -- "$d"' -- KyberWolffe roblox-studio-mcp-multisession v0.4.0-rc.5 e4f35d878024a3c73d6276bc512236e1cad8637c98894da976b233d556cd346b d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d
```

This bootstrap path is for a fresh install or reinstalling the same version.
Upgrade an existing different Multisession version only through the installed
manager's exact-tag `update` command below; direct cross-version install is
refused.

The bootstrap:

1. rejects non-macOS, non-`arm64`, and Rosetta execution before mutation;
2. downloads only assets from the exact owner, repository, and tag;
3. verifies the archive checksum and internal manifest;
4. stages the package and runs its fresh/same-version installer.

The bootstrap itself is not trusted until its fixed SHA-256 is verified. It
does not execute content from `main`.

## Manual archive verification

```bash
curl --fail --location --remote-name \
  "https://github.com/KyberWolffe/roblox-studio-mcp-multisession/releases/download/v0.4.0-rc.5/roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz"
curl --fail --location --remote-name \
  "https://github.com/KyberWolffe/roblox-studio-mcp-multisession/releases/download/v0.4.0-rc.5/roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz.sha256"
shasum -a 256 --check \
  roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz.sha256
tar -xzf roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz
python3 roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64/install.py install
```

Compare the displayed digest with the GitHub Release notes or another trusted
channel before running the installer.

The former `0.3.0-rc.3` commands remain preserved in that release's immutable
tag and documentation as historical provenance.

## Private repository

GitHub requires authentication to download private release assets. Keep that
authentication outside Multisession:

```bash
gh auth status
gh release download v0.4.0-rc.5 \
  --repo KyberWolffe/roblox-studio-mcp-multisession \
  --pattern 'roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz*'
```

Then verify and install exactly as above, or pass the local files to the
installed manager:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" \
  update \
  --tag v0.4.0-rc.5 \
  --archive ./roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz \
  --checksum-file ./roblox-studio-mcp-v2-0.4.0-rc.5-macos-arm64.tar.gz.sha256 \
  --expected-sha256 d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d
```

Do not paste a GitHub token into chat, a Multisession configuration file, a
command-line argument, or an issue. Multisession does not need access to that
token.

## Maintainer release procedure

The release workflow is intentionally manual:

1. Run `python3 -B scripts/release_dry_run.py` from a clean checkout.
2. Review the changelog, release notes, audit report, archive manifest, and
   SHA-256 outputs.
3. Enable GitHub immutable releases for the repository and create an active
   `v*` tag ruleset that restricts tag updates/deletions without an Actions
   bypass.
4. Verify both policies with maintainer/admin access, then set the repository
   Actions variable `RELEASE_POLICY_ATTESTED` to the exact value
   `immutable-releases+protected-v-tags-v1`. This is a policy attestation, not a
   secret.
5. Create and push the exact signed or protected version tag.
6. In GitHub Actions, run **Release** for that exact tag.
7. Approve the protected `github-release` environment when prompted.

The workflow checks out the tag, verifies that the package version matches,
runs the full suite and repository audit, builds deterministic artifacts,
audits the archive, and performs the isolated install proof before the publish
job receives `contents: write`. The publish job refuses repositories without
the maintainer policy attestation, stages a draft with all assets, re-resolves
the protected tag against the tested commit immediately before publishing, and
verifies the resulting immutable release attestation. Branch builds never
publish.

The normal `GITHUB_TOKEN` intentionally has no repository-administration
permission, so the workflow does not use a broad token to inspect or alter
these policies. An administrator establishes them once, records the exact
non-secret attestation variable, and the protected environment controls who
may publish.

Repository maintainers should configure:

- a protected `github-release` environment with required reviewers;
- an active no-bypass tag ruleset for `v*` that restricts updates and
  deletions;
- repository-level immutable releases, enabled before the first release;
- GitHub Security Advisories;
- minimal Actions permissions by default.

GitHub notes that required reviewers for environments are plan/visibility
dependent. If they are unavailable for a private repository, the workflow is
still manual and exact-tag-gated, but the owner must use repository rules and
restrict who can run the release workflow; do not treat an unprotected
environment name as an approval gate.

The release job uses GitHub's ephemeral `GITHUB_TOKEN`; no long-lived release
secret is required.

GitHub recommends granting that token only the minimum permissions a job
needs. The workflows keep `contents: read` everywhere except the protected
publish job, where `contents: write` is required to create the release:

- [Using `GITHUB_TOKEN` in workflows](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)
- [Protected environments and required reviewers](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
- [Preventing changes to releases](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/preventing-changes-to-your-releases)
- [Available tag-ruleset rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)

## Update and rollback

The initial rc.4-to-rc.5 migration is invoked through rc.4's former manager
name. After rc.5 activates, use the canonical manager for later operations.

Exact rc.4-to-rc.5 online update:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-v2-manage" \
  update \
  --owner KyberWolffe \
  --repo roblox-studio-mcp-multisession \
  --tag v0.4.0-rc.5 \
  --expected-sha256 d279d1f6c9b3f075b176efd4e98e543053ccd0fff5e99a8be2d7f949012b559d
```

Rollback to a retained installed version:

```bash
"$HOME/Library/Application Support/RobloxStudioMCPv2/bin/roblox-studio-mcp-multisession-manage" \
  rollback \
  --to-version PREVIOUS_VERSION \
  --accept-current-version CURRENT_VERSION
```

Update and rollback do not change the v1 integration. Restart Codex after a
version switch so `Roblox_Studio_Multisession` is the sole active product
registration. Restart Studio or reload local plugins when the plugin package
changes. Each place that should connect must allow HTTP requests. Once
registered, users name places/projects normally; Codex discovers and targets
the corresponding `studio_id` internally. Duplicate or unsaved names may
require a brief user choice, but never an active-Studio selection.

## CI platform

Workflows use GitHub's explicit `macos-15` label and assert `arm64` at runtime.
GitHub lists `macos-15` as a standard M1 arm64 hosted runner for public and
private repositories and states that GitHub-provided actions support its arm64
macOS runners. CI uses the arm64-supported Python 3.11 and 3.13 tool-cache
versions; the product's Python 3.9 minimum remains locally tested because
GitHub removed Python 3.9/3.10 from arm64 macOS runner images.

- [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub runner-image Python removal notice](https://github.com/actions/runner-images/issues/10812)

Third-party actions are not used in the release path. GitHub's checkout,
Python setup, and artifact actions are pinned to exact release commit SHAs.
