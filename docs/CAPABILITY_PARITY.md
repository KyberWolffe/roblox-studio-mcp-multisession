# V1 capability parity

<!-- parity-matrix: config/v1-capability-parity.json -->
<!-- p0-parity: incomplete -->
<!-- full-parity-claimed: false -->

Version `0.4.0-dev.1` is an isolated experimental development line. Full
25-tool parity is not claimed. It preserves the frozen rc.4 multi-session
architecture while Phase 2 addresses the 12 P0 gaps below.

This matrix covers exactly the first 25 modern tools in
`config/tool-catalog.json`. The six trailing legacy reference-plugin aliases
are excluded: `GetConsoleOutput`, `GetStudioMode`, `InsertModel`, `RunCode`,
`RunScriptInPlayMode`, and `StartStopPlay`.

Status meanings:

- `v2 full`: the required capability is available through an explicitly
  targeted v2 tool.
- `v2 partial`: a safe explicitly targeted subset exists, but the upstream
  operation shape is not complete.
- `native Codex equivalent`: the capability belongs to Codex rather than a
  Studio session and has a native Codex replacement.
- `deferred`: no approved equivalent is exposed in this release.

There is no global v1 fallback. A missing v2 capability fails closed. An
operator may deliberately use v1 for a single unsupported action only after
stopping concurrent Studio work and confirming the intended window; v2 never
delegates automatically.

## Exact 25-tool matrix

| Tool | Priority | Status | P0 gap | Current route | Phase 2 requirement |
|---|---:|---|:---:|---|---|
| `http_get` | P1 | native Codex equivalent | No | Codex web browsing | None |
| `character_navigation` | P0 | deferred | **Yes** | None | No bounded session-local character navigation adapter exists. |
| `user_mouse_input` | P0 | v2 partial | **Yes** | `studio_fire_input_binding_v2` | V2 can fire an existing Scriptable InputBinding but cannot inject arbitrary mouse or Studio UI input. |
| `search_game_tree` | P0 | v2 partial | **Yes** | `studio_list_tree_v2` | V2 now provides exact-path traversal, bounded literal name/ClassName-or-IsA filters, scan/output caps, and session-fenced cursor pagination. It still differs from the upstream multi-keyword parser, dot-path/depth-10/head-limit shape, and depth-limit child summaries. |
| `script_search` | P0 | v2 partial | **Yes** | `studio_list_tree_v2` | Tree inspection can locate known paths but there is no bounded fuzzy script-name search adapter. |
| `upload_image` | P1 | deferred | No | None | No reviewed authenticated Roblox asset-upload adapter exists. |
| `script_read` | P0 | v2 full | No | `studio_read_script_v2` | None |
| `insert_asset` | P1 | deferred | No | None | No bounded asset insertion adapter or permission contract exists. |
| `subagent` | P1 | native Codex equivalent | No | Codex multi-agent delegation | None |
| `multi_edit` | P0 | v2 partial | **Yes** | `studio_update_script_v2` | V2 provides whole-source compare-and-swap updates but not the upstream multi-patch edit shape. |
| `get_studio_state` | P0 | v2 partial | **Yes** | `studio_get_state_v2` | V2 reports normalized lifecycle state, raw controller predicates, and the sole routable Edit DataModel channel. General Server/Client operation channels remain unavailable; the PlayServer bridge is lifecycle-only and is intentionally not advertised as a route. |
| `search_asset` | P1 | deferred | No | None | Web search is not equivalent to Creator Store and authenticated inventory search. |
| `execute_luau` | P0 | deferred | **Yes** | None | Arbitrary Luau evaluation is intentionally absent pending a reviewed capability and authorization design. |
| `skill` | P1 | deferred | No | None | No Roblox-specific skill content equivalent to the v1 skill catalog is shipped. |
| `screen_capture` | P0 | v2 partial | **Yes** | `studio_capture_screenshot_v2` | Current viewport capture exists, but upstream camera-positioning and stored-image semantics are not complete. |
| `get_console_output` | P0 | v2 full | No | `studio_get_console_v2` | None |
| `script_grep` | P0 | deferred | **Yes** | None | No bounded cross-script content search adapter exists. |
| `wait_job_finished` | P0 | v2 partial | **Yes** | `get_studio_job_v2` | V2 jobs are session-scoped, but upstream primitive-generation job identity and result semantics are not adapted. |
| `generate_procedural_model` | P1 | deferred | No | None | No reviewed procedural-generation adapter exists. |
| `generate_material` | P1 | deferred | No | None | No reviewed material-generation adapter exists. |
| `user_keyboard_input` | P0 | v2 partial | **Yes** | `studio_fire_input_binding_v2` | V2 can fire an existing Scriptable InputBinding but cannot inject arbitrary keyboard or Studio UI input. |
| `start_stop_play` | P0 | v2 full | No | `studio_start_stop_play_v2` | None |
| `generate_mesh` | P1 | deferred | No | None | No reviewed mesh-generation adapter exists. |
| `inspect_instance` | P0 | v2 partial | **Yes** | `studio_list_tree_v2` | Tree metadata is available, but the full property, attribute, and child inspection shape is not. |
| `store_image` | P1 | deferred | No | None | No safe equivalent of the upstream local-file IMAGEID storage mechanism exists. |

## Publication gate

The validator requires:

- this exact 25-name set and order;
- the exact six excluded aliases;
- every v2 reference to exist in the durable or broker catalog;
- every native reference to be an approved Codex capability;
- every partial/deferred entry to state its gap;
- P0 gap flags to match status;
- `no_global_v1_fallback: true`;
- a prerelease application version and tag while any P0 gap remains;
- the negative parity markers above and no positive full-parity claim in public
  documentation.

Run it directly:

```bash
python3 scripts/validate_capability_parity.py
```
