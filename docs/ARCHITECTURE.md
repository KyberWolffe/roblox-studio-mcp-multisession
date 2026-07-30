# Roblox Studio MCP Multisession architecture

## Topology

```text
Codex task 1 ─ stdio MCP frontend ─┐
Codex task 2 ─ stdio MCP frontend ─┤
             ...                   ├─ authenticated loopback ─ Multisession v2 broker
Codex task N ─ stdio MCP frontend ─┘                         │
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

## Public name and compatibility boundary

The public product is **Roblox Studio MCP Multisession**, with short display
name **Studio MCP Multisession** and Codex server name
`Roblox_Studio_Multisession`. Version 0.4.0-rc.5 migrated the owned Codex
registration and added the canonical public launcher/display identities.
The isolated 0.4.0-rc.7 line retains that identity while correcting rc.6's
successful-creation cleanup admission with a distinct bounded authorization.

The `_v2` tool suffixes, `/v2` authenticated routes, Python package/internal
identifiers, support root, plugin filename, former launcher aliases, archive
basename, and manifest formats remain compatibility identities. They are
deliberately unchanged so the immutable `0.4.0-rc.4` bootstrap and transaction
journal can update to rc.5 and restore `0.4.0-rc.4` byte-for-byte. This split
does not create a second active server: the owned former
`Roblox_Studio_v2` Codex table is replaced by the canonical table, never
retained beside it.

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
multi-edit transaction/recovery and successful-creation cleanup ledgers
metadata
Play-transition state
```

Mode, console, jobs, pending requests, lifecycle transitions, and locks are
never global. Discovery returns non-secret snapshots only.

## Studio-side integration

Each Studio window runs an independent instance of the Multisession plugin.
Its physical package retains the legacy v2 filename for rollback
compatibility. The plugin exits before registration in a Play client/server
DataModel. The durable package has no place allowlist or window-count ceiling;
every eligible Edit DataModel creates its own runtime identity and registers
independently.

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

## Multi-edit transactions

Revision-protected multi-edit reuses the session's exclusive operation lane;
there is no global transaction lock. Writes for one `studio_id` therefore
remain FIFO-serialized, while a different Studio session can prepare or apply
its own transaction concurrently.

The broker, not the caller, owns transaction phases. One public
`studio_multi_edit_v2` call is decomposed into an internal prepare request and
a separately correlated apply request. A transaction record binds:

```text
studio_id
client_instance_id
document_epoch
generation
transaction_id
prepare_request_id
prepare_sha256
normalized bounded edit/create arguments
ordered edit and create paths plus expected/prepared/planned states
downstream per-target acknowledgements
terminal or recovery-required outcome
```

Prepare resolves and validates every existing exact script path, revision,
ordered edit, expanded replacement span, source limit, and final revision
without writing. It also resolves every exact create parent, proves the new
full path absent, validates the allowlisted script class and initial source,
and rejects any parent that another entry in the same transaction would create.
Apply rechecks every revision, parent, and absence assertion before its first
write. It updates existing targets in input order, then creates new targets in
create-input order. Each update or creation is read back. Studio positively
rechecks Edit mode and document identity at each yielding source-update commit
boundary, inside its compare-and-swap callback, immediately before parenting a
new script, and immediately before any compensating restore or destruction.
The exact path must still resolve to the prepared Instance at those boundaries.

A later failure triggers compensating rollback only under transaction-proven
state. An existing edit is restored only while its observed SHA-256 still
equals the transaction's planned revision. A created script is destroyed only
when the retained same-generation Instance is still the unique object at its
exact path and its name, class, source bytes, and SHA-256 still match the
prepared plan and it has zero children, zero attributes, and zero tags. A
moved, renamed, edited, decorated, replaced, ambiguous, or unavailable created
object is not deleted and makes recovery remain fail-closed.

This provides all-target preflight plus per-target CAS; Roblox exposes no
transaction primitive spanning multiple script sources, so v2 does not claim
cross-script atomicity. If every target cannot be proven applied, untouched,
or compensatingly restored, the exact transaction enters recovery-required
state and the session's mutation lane is quarantined. Recovery accepts only
that `transaction_id` under its original Studio/client/document/generation
identity. It cannot accept a replacement plan or replay across reconnect.
An exact same-generation recovery may return a fresh
`cached_safe_terminal` receipt only for a previously proven
`aborted_preflight`, `rolled_back`, or `recovered` terminal receipt; it carries
that prior outcome and prior receipt SHA-256 explicitly. Live recovery uses
`live_recovery` evidence and cannot substitute cached evidence. There is no
general script or Instance deletion handler.

Successful creation and uncertain-mutation recovery are separate state
machines. After an `applied` receipt with creates is validated, normal recovery
closes and one session-local cleanup grant retains only the transaction's exact
created targets plus the Studio/client/document/generation, prepare, apply, and
receipt identities. The caller receives only the transaction, apply receipt
SHA-256, and cleanup authorization SHA-256. It cannot widen the retained
targets.

While the ten-minute grant is available, bounded reads and edit-only
multi-edit remain allowed; other mutations and a second create-bearing
transaction are fenced. `studio_cleanup_multi_edit_v2` revalidates all retained
targets before the first destruction. A moved, changed, decorated, replaced,
ambiguous, or unavailable target makes the whole preflight `refused` without a
new deletion. A post-exposure property-change latch and bounded mutable-property
fingerprint supplement the exact path, class, source, child, attribute, and tag
checks. If a dispatched cleanup becomes partial or unproven, the session enters
cleanup quarantine. Only the same exact cleanup in the same generation can
reconcile already-absent targets and remaining unchanged targets before the
original absolute ten-minute deadline. Expiry never renews on retry: an unused
grant retires, while a dispatched or required grant becomes settlement-only
quarantine that rejects fresh deletion dispatch but can still validate safe
terminal evidence from an already-dispatched request. A reconnect,
wrong-session/hash reuse, or consumed grant never rebinds cleanup. Lifecycle
health reports available, dispatched, required, or expired-settlement cleanup
state as an explicit stop blocker until it is safely retired.

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
exactly fifteen audited operations: state, tree, fixed-allowlist instance
inspection, script-name search, literal cross-script grep, script read/update,
revision-protected multi-edit, exact-transaction recovery, separate exact
transaction-created cleanup, attribute update, console, screenshot, Scriptable
`InputBinding`, and Play/Stop.
Instance inspection is an Edit-only observational snapshot of one exact
unambiguous path. It uses bounded closed value encoding and does not reflect
arbitrary properties, read source, or expose security identities. At the
public MCP boundary the catalog:

1. preserves descriptions and annotations;
2. injects a required UUID `studio_id`;
3. maps only to an exact implemented Studio-side handler.

The bounded instance metadata operations use the documented
[Roblox Instance API](https://create.roblox.com/docs/reference/engine/classes/Instance)
for children, attributes, and tags. The adapter sorts returned collections
itself and does not treat API enumeration order as a durable contract.

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

Job admission freezes a deep copy of the already schema-validated arguments
and binds the exact Studio/client/document/generation identity, durable
handler, handler/input/output schema hashes, argument hash, admission
sequence, request IDs, and terminal outcome. The public job allowlist contains
only operations with a closed host-side result validator. A response is
validated against both that handler contract and the admitted identity before
it can be retained or delivered.

Same-session direct calls and jobs share one FIFO admission lane; a job cannot
overtake a direct call accepted earlier for the same Studio. Different
sessions own independent admission lanes and can overlap.

Queued jobs may be cancelled. A dispatched job keeps the Studio lock until its
response, timeout, or disconnect. Without an acknowledged downstream cancel
protocol, v2 refuses to report a dispatched mutation as cancelled. A
multi-edit recovery appends an identity-bound resolution receipt to the
original job without replacing its original apply request, result digest, or
terminal evidence.

The in-memory job ledger is bounded by active count, retained count, and
retained result bytes. Only positively terminal jobs with no active,
uncertain, or recovery-required work can retire. Retirement creates a bounded,
hash-chained, non-secret tombstone containing contract and outcome digests;
active or uncertain records are never compacted.

## Coexistence with production v1

Multisession is a separate broker endpoint, plugin artifact, and optional
Codex MCP entry. The existing v1 binary, plugin, configuration, and live
process are left intact as a fallback.

Wrapping v1 by selecting a Studio before each request would reintroduce the
race this design removes. V2 operational traffic must therefore go directly
to the session that owns the explicit `studio_id`; it never delegates routing
to v1’s active-Studio selector.
