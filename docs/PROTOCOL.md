# Local v2 protocol

All endpoints are HTTP on an explicit loopback address. Studio controller
endpoints use a Studio-registration bearer token plus the session’s rotating
resume credential. MCP client endpoints use a different bearer token. The
Play server starts with a one-time transition token and receives a separate
derived server token. Requests with `Origin` are rejected.

`studio_id` is routing context, not authorization. Every operational route
also verifies the appropriate credential, generation, document context, and
capability.

## Studio registration

First connection:

```json
{
  "client_instance_id": "runtime-generated-plugin-uuid",
  "registration_secret": "high-entropy-plugin-secret",
  "document_epoch": "runtime-generated-document-uuid",
  "metadata": {
    "name": "Disposable Baseplate A",
    "place_id": 0,
    "mode": "edit"
  },
  "capabilities": [
    "studio_get_state",
    "studio_multi_edit",
    "studio_recover_multi_edit"
  ]
}
```

`POST /v2/studios/connect` returns a server-assigned `studio_id`,
`generation: 1`, and a secret `resume_token`.

Reconnect repeats the request with `studio_id` and the last `resume_token`.
It may also provide `settled_request_ids`, taken from the plugin’s local
execution ledger, for previously dispatched operations it can prove have
terminated. The document epoch must match. The response increments
`generation` and rotates the token. An unknown requested ID cannot be claimed.

`client_instance_id` identifies one physical plugin runtime and is bound to a
separate high-entropy `registration_secret`. Retrying an initial registration
before the first poll is idempotent and returns the same Studio ID/token. Once
polling begins, a duplicate fresh registration is rejected. A new document
epoch requires the prior document session to disconnect; the retired ID cannot
later resume. Replacing a still-live generation is rejected until its lease is
stale or it disconnects explicitly.

Changing or reloading a place creates a new document epoch and new session
instead of silently reusing the old target.

## Poll and request

`POST /v2/studios/poll` includes:

```json
{
  "studio_id": "8a9daed7-f9a9-4d52-8382-fb9727dbe3f1",
  "generation": 3,
  "resume_token": "secret"
}
```

A queued operation is returned as:

```json
{
  "v": 2,
  "kind": "request",
  "studio_id": "8a9daed7-f9a9-4d52-8382-fb9727dbe3f1",
  "document_epoch": "document-a",
  "generation": 3,
  "request_id": "f5f1403f-2f12-4225-b1ac-8dd8c87295f4",
  "operation": "studio_multi_edit",
  "args": {},
  "deadline_ms": 30000
}
```

Poll timeout is a successful response with a null result.

## Response, events, and disconnect

`POST /v2/studios/response` repeats connection routing fields and correlation:

```json
{
  "studio_id": "8a9daed7-f9a9-4d52-8382-fb9727dbe3f1",
  "generation": 3,
  "resume_token": "secret",
  "request_id": "f5f1403f-2f12-4225-b1ac-8dd8c87295f4",
  "success": true,
  "result": {}
}
```

`POST /v2/studios/event` additionally includes `event_type` and `payload`.
Mode, console, and job events are accepted only for the current generation and
update only that session.

`POST /v2/studios/disconnect` requires the current ID, generation, and resume
credential. A stale disconnect cannot close a replacement connection.

If connection loss or timeout leaves a dispatched request without a proven
terminal outcome, the session is quarantined. It accepts no new Studio-bound
work until either a same-generation late response arrives or an authenticated
reconnect includes that request in `settled_request_ids`.

## MCP frontend API

The thin frontend uses authenticated `/v2/client/*` endpoints:

- `tools`: approved schemas with explicit targeting injected;
- `list`: authorization-filtered discovery;
- `call`: public tool name, arguments including `studio_id`, and an internal
  request-correlation UUID;
- `jobs/start`, `jobs/get`, `jobs/cancel`: all explicitly targeted.

Missing, malformed, unknown, disconnected, or unauthorized IDs fail closed.
The router never substitutes another session, even when exactly one Studio is
connected.

## Direct and job contract receipts

The host validates each public call against the exact closed input schema
stored for its durable handler before it creates a job or sends a Studio
request. Job admission then freezes the validated arguments and records:

```text
studio_id
client_instance_id
document_epoch
generation
job_id
admission_sequence
operation and durable handler
input/output/handler schema SHA-256 values
canonical argument SHA-256
phase request IDs
cancellation state
terminal outcome and result SHA-256
```

Only state, tree, script-name search, literal grep, instance inspection,
multi-edit, and exact multi-edit recovery are job-admissible. This is an exact
allowlist: operations without a complete closed host result validator are
rejected before a job record or downstream request is created.

Direct calls and jobs share one FIFO admission sequence per session. Different
`studio_id` sessions have independent sequences and locks. A returned job
receipt preserves the admitted Studio/client/document/generation identity and
schema hashes; metadata drift or an impossible handler result is rejected
before delivery or retention.

An exact recovery of a job-backed multi-edit appends a bounded resolution
receipt containing the recovery request identity, validated recovery result,
result digest, and terminal resolution. It does not overwrite the original
apply request/result digest. Terminal jobs may retire only when no associated
request is active, uncertain, or recovery-required. Retirement emits a
bounded hash-chain tombstone; uncertain or active evidence is never
compacted.

## Revision-protected multi-edit

`studio_multi_edit_v2` is an Edit-only mutation addressed to one explicit
`studio_id`. Its public arguments contain exactly `studio_id`,
`datamodel_type: "Edit"`, and `targets`; transaction phase and credentials are
broker-owned and cannot be supplied by the caller. Each of the 1-16 targets
contains:

```json
{
  "path": ["ServerScriptService", "RoundController"],
  "expected_sha256": "64-lowercase-hex-source-revision",
  "edits": [
    {
      "old_string": "local limit = 4",
      "new_string": "local limit = 6",
      "replace_all": false
    }
  ]
}
```

Paths are nonempty arrays of exact child-name segments and must be unique
within the transaction. Targets execute in input order. Each target's 1-64
edits executes in input order against the result of its preceding edit.
`old_string` is nonempty and must differ from `new_string`. Optional
`start_byte` and `end_byte` are a paired, zero-based, half-open UTF-8 byte
range that must contain exactly `old_string`; a ranged edit cannot set
`replace_all: true`. Overlapping expanded matches, ambiguity, stale revisions,
invalid UTF-8, creation, and out-of-range offsets fail before mutation.

Both host and Studio independently enforce 16 targets, 64 edits per target,
128 edits total, 1024 expanded replacement spans, 8192 aggregate UTF-8 path
bytes, 262144 aggregate literal bytes, 350000 canonical public argument bytes,
262144 source bytes per target, 1048576 aggregate bytes each for original and
planned source, and 100000 encoded receipt bytes. The private phase envelope
allows 351000 bytes so host-added transaction fields cannot invalidate a
maximally bounded public request.

The mutation is a broker-owned two-phase transaction:

1. The broker normalizes the entire plan, binds a fresh `transaction_id` to
   `(studio_id, client_instance_id, document_epoch, generation)`, and dispatches
   an internal prepare request.
2. Studio resolves every exact path, reads every source, checks every expected
   SHA-256, expands every edit deterministically, and returns a closed prepare
   receipt. It has not mutated source at this point.
3. The broker validates the complete identity, target order, paths, revisions,
   counts, planned revisions, and canonical `prepare_sha256`; any drift aborts.
4. An apply request carries only the broker-owned prepared transaction. Studio
   rechecks every target before the first write, then applies per-target
   compare-and-swap updates in input order.
5. Every downstream update is read back. If a later target fails, Studio
   attempts compensating rollback of already applied targets only when their
   revisions still match the transaction's planned revisions.

The prepare receipt is internal and never returned as a successful public
multi-edit result. It binds the identity tuple, prepare request and transaction
IDs, `target-input-edit-input-v1` ordering, the
`preflight-all-per-target-cas-compensating-no-cross-script-atomicity-v1`
atomicity statement, aggregate source bounds, and per-target expected,
prepared, and planned revisions and counts.

The public result is a closed, broker-validated downstream receipt. It repeats
the exact Studio/client/document/generation/request/transaction identity,
`prepare_request_id`, `prepare_sha256`, ordering and atomicity versions,
`broker-validated-downstream-ack-v1`, per-target before/after revisions and
statuses, and a canonical `receipt_sha256`. Target receipt `index` values are
one-based and exactly follow caller target order. Apply outcomes are:

- `applied`: every target was applied and acknowledged;
- `aborted_preflight`: the apply recheck found drift before the first write;
- `rolled_back`: a partial apply was compensatingly restored and acknowledged;
- `recovery_required`: at least one dispatched mutation cannot be proven
  applied or restored.

This is intentionally not cross-script atomicity. A safe terminal outcome is
reported only when all target revisions prove it. `recovery_required` marks
the transaction and session mutation lane quarantined. The caller may invoke
`studio_recover_multi_edit_v2` with the same explicit `studio_id` and exact
`transaction_id`; recovery cannot accept new edit text or rebind the
transaction to another client, document, or generation. Its only outcomes are
`recovered` after every target is positively terminal, or
`recovery_required`. Disconnects and generation changes never cause replay or
manufactured success.

## Play bridge context

Every Play bridge message carries the same immutable transition context:

```text
studio_id
client_instance_id
document_epoch
transition_generation
play_request_id
expected_place_id
expected_game_id
transition_nonce
```

The context binds the transition to one authenticated plugin runtime, one
document, one connection generation, one Play-start request, and one expected
Roblox place/universe. The nonce is a canonical UUID generated by the broker.
There is at most one incomplete transition per `studio_id`; other Studio IDs
have independent transition records.

### Controller routes

The Edit-context plugin uses its current Studio bearer and resume credentials:

- `POST /v2/studios/play-bridge/prepare`
- `POST /v2/studios/play-bridge/abort-pre-attach`
- `POST /v2/studios/play-bridge/status`
- `POST /v2/studios/play-bridge/request-stop`
- `POST /v2/studios/play-bridge/complete`

`prepare` is accepted only while the matching, explicitly targeted
`rnd_play_start` request is pending. It returns the transition context and a
one-time `bridge_token`. A safe pre-attach abort is possible only when the
runner provably never started and the temporary Script has been cleaned.

`request-stop` binds the controller request to an exact `stop_command_id`.
Repeated delivery of the same controller stop is idempotent; a conflicting
stop owner is rejected.

`complete` requires:

- a recognized completion outcome;
- the exact `EndTest` correlation;
- proof that the yielding runner returned;
- two to ten stable Edit confirmations;
- proof that the exact temporary Script was cleaned.

For a requested Stop, completion additionally requires prior
`watchdog_armed` and correlated `stop_received` acknowledgements. Natural
completion is accepted only for an attached, watchdog-acknowledged transition
with no stop command. A start-failure outcome is valid only before server
attachment.

### PlayServer routes

The fixed PlayServer plugin bridge uses only:

- `POST /v2/play-bridge/attach`
- `POST /v2/play-bridge/server-poll`
- `POST /v2/play-bridge/server-ack`

The one-time bridge token authenticates `attach`. The broker binds a fresh
`server_instance_id`, derives a transition-specific `server_token`, and burns
the bootstrap credential when that server credential is first used.

The PlayServer plugin acknowledges `watchdog_armed` before polling for Stop. When
`server-poll` returns the exact stop command, the server acknowledges
`stop_received` with the same command ID before invoking
`StudioTestService:EndTest()`.

Play operations use a longer broker deadline than ordinary Studio calls. While
the Edit-context plugin starts a slow runner, it first prepares one exact
transition, schedules `ExecutePlayModeAsync`, and returns a correlated
`starting` receipt. Stop similarly binds one exact stop command and returns a
correlated `stopping` receipt. Read-only state observations then report
`starting`, `play`, `stopping`, `settling`, terminal Edit proof, or
`recovery_required`; they never replay Start or Stop.

The plugin monitors the transition at a bounded interval while server startup
is pending. Its authenticated status reads renew only that session's lease.
The one-time bridge token has a bounded pre-attach TTL, and the exact server's
watchdog acknowledgement grants a fresh bounded active TTL so slow startup
does not consume usable Play time. If the Studio controller disconnects, the
broker can still return its non-secret transition state for safe recovery.

Stale tokens, reused attach identities, wrong server identities, replayed
acknowledgements, mismatched stop IDs, or fields from another Studio fail
closed.

## Play reconnect and watchdog behavior

Controller disconnect, generation replacement, or transition TTL expiry moves
an incomplete transition toward Stop and marks it recovery-only. The original
`transition_generation` remains in the immutable transition context; the new
current generation must authenticate controller recovery calls. This prevents
a reconnect from relabeling or replaying the old lifecycle action.

Host watchdog expiry can request Stop and record an expiry reason. The
PlayServer plugin bridge independently arms a hard bounded `EndTest` watchdog
before its first HTTP request. The disabled temporary Script is only a
transition-owned marker and never performs HTTP. Neither watchdog may report
completion on its own.
Only the Edit-context runner can complete the transition after validating the
returned result, cleaning the Script, and observing stable Edit mode.

Normal operations for that Studio remain fenced while lifecycle state is
uncertain. Other `studio_id` sessions remain independent.
