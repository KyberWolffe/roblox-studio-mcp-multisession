# Changelog

This project uses semantic versioning for the broker, installer, and Studio
plugin release. The reviewed upstream Roblox tool-catalog version is tracked
separately and is reported by `doctor`.

## 0.3.0-rc.2 — 2026-07-26

Security and CI correction to the unpublished `0.3.0-rc.1` candidate.

### Fixed

- Authenticated loopback HTTP now uses a direct, proxy-free opener and refuses
  redirects. Ambient proxy variables can no longer receive bearer credentials
  or prevent local broker readiness.
- The disposable mock Studio applies the same loopback-only, proxy-free,
  no-redirect transport boundary.
- Broker binding bypasses Python's unnecessary reverse-DNS lookup for an
  already validated literal loopback address, so slow local name resolution
  cannot exhaust the bounded startup deadline.
- GitHub workflows suppress bytecode for every project Python command and
  audit the checkout before and after tests, preventing `__pycache__` from
  contaminating deterministic source audits.

## 0.3.0-rc.1 — 2026-07-26

Unpublished initial release-candidate packaging of the live-validated 0.2.0
multi-session architecture.

### Added

- Apple Silicon macOS-only platform gate, including fail-before-mutation
  detection for Intel Macs and processes running through Rosetta.
- Standalone, reproducible GitHub release packaging and SHA-256 manifests.
- Pinned-release bootstrap, staged update, retained-version rollback, and
  offline/private-repository update paths.
- Repository and archive audits for credentials, private runtime material,
  machine-specific paths, and unsafe package entries.
- Isolated temporary-home proof covering install, status/doctor, no-op repair,
  damaged-component repair, update/rollback, and uninstall restoration.
- Apple Silicon GitHub Actions CI and manually gated tag-release automation.
- Exact 25-tool v1 capability parity ledger, with an enforced prerelease gate
  while P0 gaps remain and no global v1 fallback.

### Preserved from 0.2.0

- Explicit `studio_id` on every Studio operation, with no active/default
  Studio fallback.
- Per-session locking, mode, console, job, correlation, reconnect, and
  lifecycle state.
- Concurrent operation of distinct Studio sessions and serialization of
  conflicting work within one session.
- Authenticated Play/Stop bridge with transition nonces, replay protection,
  bounded watchdogs, and observed return to Edit mode.
- Side-by-side Codex and Roblox Studio installation that leaves v1 untouched.
- Review-gated upstream catalog compatibility workflow.

### Compatibility

- Supported: native Apple Silicon macOS (`arm64`) with Python 3.9 or newer.
- Unsupported: Intel Macs, `x86_64` processes, and Rosetta execution.

## 0.2.0 — validated baseline

The durable broker, explicit-targeting MCP frontend, Studio plugin, installer,
catalog workflow, and Play/Stop lifecycle were completed and validated in an
isolated R&D environment. Version 0.3.0-rc.1 changes distribution and lifecycle
management; it does not widen the Studio operation surface.
