# Contributing

Thank you for considering a contribution. The repository currently has no
public software license; see [LICENSE_STATUS.md](LICENSE_STATUS.md). Do not
submit third-party code, schemas, assets, or Roblox content unless you have the
right to contribute it and its terms are documented.

## Development requirements

- Native Apple Silicon macOS; Intel and Rosetta are intentionally unsupported.
- Python 3.9 or newer.
- No third-party Python dependencies are required for the core suite.

Create a branch, keep changes focused, and run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
python3 scripts/validate_capability_parity.py
python3 scripts/audit_release.py --repo .
python3 scripts/release_dry_run.py
```

The release dry run builds the same deterministic archive used in GitHub
Actions, audits it, and exercises installation only in a temporary home. It
does not modify a real Codex configuration, Roblox plugin directory, or Studio
place.

## Security and compatibility expectations

- Every new operational Studio tool must require `studio_id`.
- No handler may consult a global active/default Studio.
- New operation shapes fail closed until an explicit adapter, Studio-side
  handler, authorization classification, and isolation tests exist.
- Same-session lifecycle or mutation conflicts must serialize; different
  sessions must remain independent.
- Never commit credentials, rendered credential-bearing plugins, live
  session state, logs, rollback receipts, local absolute paths, or production
  place identifiers.
- Do not weaken loopback binding, credential separation, request correlation,
  reconnect fencing, bounded payloads, or permission checks.

Run live Studio tests only against explicitly authorized disposable places,
starting with the documented single-session Play/Stop gate. Live tests are not
part of pull-request CI.

## Pull requests

Describe the invariant being changed, the tests added, and any security or
rollback impact. Generated catalogs and plugin output must be reproducible from
committed sources. A tool-catalog change must include the compatibility diff,
fixture update, generated durable catalog, and contract tests.
