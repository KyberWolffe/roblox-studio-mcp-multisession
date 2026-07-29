# Architecture

## Topology

```text
Codex task 1 ─ stdio MCP frontend ─┐
Codex task 2 ─ stdio MCP frontend ─┤
             ...                   ├─ authenticated loopback API ─ v2 broker
Codex task N ─ stdio MCP frontend ─┘                              │
                                                                 ├─ session/lock A ─ Studio A plugin
                                                                 ├─ session/lock B ─ Studio B plugin
                                                                 └─ session/lock N ─ Studio N plugin
```

The shared broker is the concurrency authority. A lock inside each Codex
frontend would be insufficient because separate tasks are separate processes.
Frontends generate internal correlation UUIDs and never own Studio selection,
Studio state, or operation locks.

The broker uses a dynamic map keyed by exact, server-assigned UUID. It has no
operational active/default/current Studio value. `list_roblox_studios_v2` is
discovery only. Every other MCP schema, including job start/get/cancel, has
`studio_id` in `required`.

There is no architectural two-session limit. Automated coverage registers
more than two sessions against the same registry and scheduler. Practical
capacity is bounded by Studio processes, local HTTP polling, memory, and CPU,
not a configured session count.

## Session state

Each `StudioSession` owns:

```text
studio_id
client_instance_id
document_epoch
generation
resume-credential hash
connection/transport
approved advertised capabilities
exclusive operation queue
pending request map
mode and outcome uncertainty
console sequence/buffer
job map
metadata
Play-transition state
```

Mode, console, jobs, pending requests, lifecycle transitions, and locks are
never global. Discovery returns non-secret snapshots only.

## Studio-side integration

Each Studio window runs an independent instance of the side-by-side v2 plugin.
The plugin exits before registration in a Play client/server DataModel. The
durable package has no place allowlist or window-count ceiling; every eligible
Edit DataModel creates its own runtime identity and registers independently.

Within an Edit DataModel, the plugin generates a fresh:

- `client_instance_id`;
- high-entropy `registration_secret`;
- `document_epoch`.

Those values are window-local and are not stored in global plugin settings.
The plugin advertises only its closed capability set, registers with the
broker, receives a server-assigned `studio_id`, and long-polls using a
generation-bound rotating resume token.

The durable renderer packages the audited Luau sources as the separately named
`StudioMCPv2SideBySide.rbxmx`. Rendering does not install the package or
modify the existing production plugin.

## Scheduling

All current Studio-bound operations are conservatively exclusive within their
target session. The production native implementation is not available to
audit for reentrancy, so unknown tools also default to exclusive.

This gives the required behavior:

- a write or Play/Stop transition for Studio A cannot overlap another A call;
- input, screenshot, console, script, read, edit, job-backed work, and unknown
  current tools also serialize for A;
- Studio B, C, and later IDs own different locks and can proceed while A is
  blocked;
- the registry map is never locked across a Studio request.

An audited future version may add a per-session shared-read lane. That would
not require a global active Studio.

## Correlation and reconnect

Ordinary request correlation is:

```text
(studio_id, document_epoch, generation, request_id)
```

The request ID is generated at the frontend/broker boundary, not copied from a
raw MCP JSON-RPC ID. Separate tasks may therefore reuse their external IDs
without collision.

One authenticated `client_instance_id` maps to at most one current document
session. The initial connect is idempotent until first poll, preventing a lost
registration response from producing a second independently locked ID. New
document epochs retire the old mapping, and a live generation cannot be
silently taken over before disconnect or lease expiry.

A queued call captures `generation` before waiting for its session lock and
checks it again after admission. Disconnect rejects in-flight requests. A
valid reconnect:

1. proves possession of the current resume credential;
2. matches the same `document_epoch`;
3. rotates the resume credential;
4. increments `generation`;
5. closes the old transport and rejects its pending work.

Old responses/events, stale close operations, and pre-reconnect queued calls
cannot affect the replacement connection. Nothing is silently replayed.
Any dispatched request without a proven terminal response remains in an
uncertainty ledger. The session is quarantined until a late
current-generation response or an authenticated plugin settlement ledger
clears it.

## Play/Stop lifecycle

Roblox documents `StudioTestService:ExecutePlayModeAsync()` as a yielding
Plugin Security API and `StudioTestService:EndTest()` as the corresponding test
completion API. The v2 plugin uses this supported lifecycle because its
PlayServer plugin runtime can receive the exact test args and return a
structured result to the original Edit-context runner. Plugin-context
loopback access is independent of the place's saved HTTP setting.

The transition is keyed by:

```text
(studio_id, client_instance_id, document_epoch, transition_generation,
 play_request_id, expected_place_id, expected_game_id, transition_nonce)
```

The flow is:

1. The broker prepares a transition only for the currently pending,
   explicitly targeted `rnd_play_start`.
2. The plugin creates one nonce-named, attributed, disabled temporary server
   Script as an inert ownership marker and starts
   `ExecutePlayModeAsync(bootstrap)`.
3. The plugin returns a correlated `starting` receipt and monitors that exact
   nonce asynchronously, allowing read-only state calls while Studio loads.
4. The installed plugin loads in the PlayServer DataModel, validates the exact
   bootstrap and inert marker, attaches once with the bootstrap token,
   receives a derived server token, and acknowledges `watchdog_armed`.
5. Only after that acknowledgement does observation report Play ready.
6. A same-session Stop creates an exact `stop_command_id` and returns a
   correlated `stopping` receipt. The server polls it, acknowledges
   `stop_received`, and calls `EndTest()` with that correlation.
7. The original runner validates the result, removes the exact temporary
   Script, observes multiple stable Edit samples, and submits the completion
   proof.

The host has one lock and one active transition per `studio_id`, while different
Studio IDs have independent transition records and may proceed concurrently.
Stale/replayed nonces, acknowledgements, server identities, generations, and
completion proofs are rejected.

Bounded host and Play-server watchdogs attempt to drive an incomplete
transition toward Stop. They do not manufacture success: the transition is
not complete until the runner returns and Edit mode is observed. If the plugin
disconnects or reconnects mid-transition, the original transition context is
retained as recovery-only and normal work remains fenced until it terminates.
The broker can still return a non-secret recovery view containing the exact
transition state, so a disconnected controller does not make observation
depend on guessing. A disconnected record is lifecycle-safe only when Edit,
zero pending/uncertain work, and terminal-or-absent Play are all positively
proven. It remains available for a bounded reconnect grace period, then is
compacted into a bounded non-secret audit tombstone. Any uncertainty or active
transition prevents retirement.

`RunService` supplies documented Plugin Security controls and mode predicates,
but `RunService:Stop()` is not used as proof of lifecycle completion. It lacks
the correlated server acknowledgement and runner-return result required by the
v2 contract.

References:

- [Roblox RunService reference](https://create.roblox.com/docs/reference/engine/classes/RunService)
- [Roblox StudioTestService reference](https://create.roblox.com/docs/reference/engine/classes/StudioTestService)

## Tool catalog

`config/durable-tool-catalog.json` is the default runtime catalog. It exposes
exactly eleven audited operations: state, tree, script-name search, literal
cross-script grep, script read/update, attribute update, console, screenshot,
Scriptable `InputBinding`, and Play/Stop. At the public MCP boundary the
catalog:

1. preserves descriptions and annotations;
2. injects a required UUID `studio_id`;
3. maps only to an exact implemented Studio-side handler.

A Studio registers only a list of capabilities. It cannot define a new public
tool. A call is rejected if the target Studio did not advertise that approved
capability.

`config/tool-catalog.json` is an isolated upstream-known snapshot used only as
input to the reviewed compatibility workflow. It is not published wholesale
by the durable broker. Unknown names, aliases, argument shapes, or operation
families fail closed; an operator must add an exact adapter and tests before a
new capability can enter the durable catalog.

## Jobs

Jobs are broker-managed and stored in their target session. Status and
cancellation require both `studio_id` and `job_id`; identical job IDs in two
sessions do not collide.

Queued jobs may be cancelled. A dispatched job keeps the Studio lock until its
response, timeout, or disconnect. Without an acknowledged downstream cancel
protocol, v2 refuses to report a dispatched mutation as cancelled.

## Coexistence with production v1

V2 is a separate broker endpoint, plugin artifact, and optional Codex MCP
entry. The existing v1 binary, plugin, configuration, and live process are
left intact as a fallback.

Wrapping v1 by selecting a Studio before each request would reintroduce the
race this design removes. V2 operational traffic must therefore go directly
to the session that owns the explicit `studio_id`; it never delegates routing
to v1’s active-Studio selector.
