# Durable side-by-side Studio plugin

The durable v2 plugin is not limited to two Studio sessions. Every plugin
runtime generates its own `client_instance_id`, registration secret, document
epoch, and derived session tag; the broker assigns a distinct `studio_id`.
The broker session map has no two-session branch or configured session-count
limit. Practical capacity is bounded by Studio processes, local HTTP polling,
memory, and CPU.

The plugin never selects an active/default Studio. Every operational MCP schema
gets a required `studio_id` at the broker publication boundary. That ID is
routing context only; the client and Studio bearer tokens, registration secret,
resume credential, generation, and authorization policy remain separate checks.

## Durable operation surface

The installed catalog exposes exactly these Studio-side handlers:

- `studio_get_state`
- `studio_list_tree`
- `studio_read_script`
- `studio_update_script`
- `studio_set_attribute`
- `studio_get_console`
- `studio_capture_screenshot`
- `studio_fire_input_binding`
- `studio_start_stop_play`

Discovery and jobs remain broker-side. Test marker and test shutdown operations
are validation-only and are not advertised by the durable plugin.

Instance paths are arrays of exact child-name segments. They are not Luau
expressions. Duplicate sibling names make a path ambiguous and fail closed.
Script updates use `ScriptEditorService:UpdateSourceAsync` with a required
SHA-256 compare-and-swap revision. Primitive attribute updates require an exact
expected prior state and confirm the result.

Screenshot capture uses the documented Studio capture APIs and returns bounded
`image_base64`, `mime_type`, `width`, and `height` fields. Missing permission,
an unsupported buffer status/format, or an oversized image fails closed.

Input is deliberately narrow: it can call `Fire` only on an existing
`InputBinding` whose `Type` is `Scriptable`, resolved by exact path. It cannot
inject keyboard, mouse, operating-system, or Studio user-interface input.

Play/Stop uses the documented Studio plugin-security test APIs plus the
authenticated server-context `EndTest` bridge. Start and Stop share
`studio_start_stop_play`, but the host binds the pending request to the exact
phase arguments (`{"is_start": true}` or `{"is_start": false}`), transition
generation, nonce, and explicit `studio_id`.

## Rendering

Rendering is a pure repository operation; it does not install a plugin or
contact Studio:

```python
from scripts.render_studio_plugin import package_rbxmx, render_durable

source = render_durable(
    studio_token,
    install_run_id,
    base_url="http://127.0.0.1:44756",
)
package = package_rbxmx(
    source,
    package_name="StudioMCPv2SideBySide",
)
```

The installer owns and reuses the Studio token and install run ID so repair can
reproduce the same source. Each running Studio still generates fresh
window-local identity and registration credentials. The renderer accepts only
an explicit `http://127.0.0.1:<port>` origin.

Initial broker absence is retried indefinitely with a bounded backoff. A normal
reconnect must retain the same `broker_instance_id`. If the broker was replaced
and has an empty session map, the plugin may register afresh only when no Play
transition is active or uncertain. It retains the same runtime client identity,
registration secret, and document epoch. During active/uncertain Play it fails
closed and relies on the bounded server watchdog; it does not claim recovery
against an empty broker.

## Reviewed upstream compatibility

Raw upstream tool names are never copied into the durable surface. The
operator-owned mapping is
`config/upstream-compatibility-map.json`. Its policy is
`exact_handler_schema`: an added, renamed, or schema-changed upstream operation
is compatible only when:

1. its exact upstream name is present in the operator mapping;
2. the mapping resolves to one of the nine existing durable handlers;
3. its schema exactly matches that handler's current local schema; and
4. the generated catalog still passes the explicit-`studio_id`, handler-source,
   provenance, and fixed-allowlist contract checks.

Unknown names and changed shapes are quarantined and fail closed. A compatible
addition remains review-only until explicit approval. The fixture workflow is
covered by:

- `tests/fixtures/upstream-catalog-baseline.json`
- `tests/fixtures/upstream-catalog-compatible-addition.json`
- `tests/fixtures/upstream-compatible-generation.json`

Review an explicit local artifact:

```sh
python3 -m scripts.review_upstream_catalog \
  /absolute/path/to/upstream-tool-catalog.json
```

Review the exact installed v1 cache. The command resolves the user's home with
the passwd database (not `HOME`) and accepts only the regular, non-symlink,
current-user-owned file at
`Library/Application Support/StudioMCP/tools-cache.json`:

```sh
python3 -m scripts.review_upstream_catalog \
  --installed-v1-cache
```

Run the full generation and publication contracts without writing anything
before asking the lifecycle manager to stop the v2 broker:

```sh
python3 -m scripts.review_upstream_catalog \
  --installed-v1-cache \
  --prepare-import \
  --regenerate-durable
```

After reviewing the report, atomically import the upstream snapshot and
regenerate only exact mapped durable schemas:

```sh
python3 -m scripts.review_upstream_catalog \
  /absolute/path/to/upstream-tool-catalog.json \
  --apply \
  --approve-reviewed-changes \
  --accept-sha256 REVIEWED_CANDIDATE_SHA256 \
  --regenerate-durable
```

The command reopens the candidate and requires its exact bytes to match the
reviewed checksum before any write. It validates both resulting catalogs and
all publication contracts before replacing either target. It writes exact
hash-named backups and a transaction receipt. If a multi-file replacement
fails, it restores the prior bytes. Roll back a completed import with:

```sh
python3 -m scripts.review_upstream_catalog \
  --rollback-receipt /absolute/path/to/catalog-import-receipt-XXXXXXXXXXXX.json
```

Rollback refuses if either installed catalog or backup changed after the
receipt was written. It also preserves the pre-rollback bytes.

In an installed bundle, the manager supplies explicit support paths to the same
callable APIs. The effective catalog is `support/config/tool-catalog.json`; the
separate reviewed snapshot is
`support/config/upstream-known-tool-catalog.json`; and the operator mapping is
`support/config/upstream-compatibility-map.json`. The installed manager invokes
the pinned release under isolated Python flags, so import does not depend on a
repository working directory or `PYTHONPATH`.

`roblox-studio-mcp-v2 doctor --json` reports the configured catalog's local format and
version, effective canonical catalog hash, and the separate sanitized upstream
version/source hash/compatibility fields. No import command fetches a URL or
executes candidate code.

## Validation boundary

The repository suite covers dynamic session maps, different-session
concurrency, same-session serialization, exact phase binding, unpublished zero
IDs, renderer restrictions, reconnect fencing, catalog generation,
unknown-family quarantine, atomic import, and rollback. Repository validation
does not constitute a Studio runtime test. Any Studio-side handler or lifecycle
change must pass a single authorized disposable-session gate before widening
to concurrent live validation.
