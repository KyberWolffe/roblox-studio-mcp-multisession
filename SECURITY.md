# Security

## Reporting a vulnerability

Do not disclose an exploitable issue, credential, live session ID, private
place identifier, or reproduction against a production Studio place in a
public issue.

After publication, use the repository's private GitHub Security Advisory
channel. If that feature is unavailable, contact the repository owner through
a private channel they designate. Include the affected version, impact,
minimal reproduction, and whether any secret or live place was involved. Do
not send tokens.

## Trust boundaries

- The broker binds only to explicit loopback and rejects browser-origin
  requests.
- Studio and MCP clients use distinct high-entropy bearer credentials. The
  credentials may not be equal.
- First registration receives an opaque server-assigned `studio_id` and a
  separate resume credential.
- `studio_id` is routing context, not authorization. MCP scope, Studio
  credential, capability, document identity, and connection generation are
  checked independently.
- Each Studio plugin runtime owns a fresh client identity, registration secret,
  and document epoch. These are not kept in shared plugin settings.
- Reconnect rotates the resume credential and increments the session
  generation. Old transports and queued pre-reconnect work are fenced.
- The broker publishes only an operator-reviewed closed catalog. A Studio can
  advertise a subset of approved capabilities; it cannot define a public tool
  or schema.
- Request bodies and returned screenshots are bounded. Credentials and
  operational payloads are not logged.

## Session isolation

The broker maintains a dynamic map keyed by exact `studio_id`; it has no
active/default/current Studio pointer. Every session owns its own queue, lock,
mode, console, jobs, correlation state, uncertainty ledger, and Play
transition.

Current Studio operations serialize within one session. Different sessions
use different locks and may progress concurrently. Knowing another
`studio_id` does not bypass authorization.

Missing, malformed, unknown, disconnected, or unauthorized IDs fail with no
send. The router never substitutes another Studio, including when exactly one
session is connected.

Discovery is non-operational. A Codex task may use
`list_roblox_studios_v2` to resolve the user's place/project name, but every
subsequent operation still carries the exact discovered ID. Duplicate names,
unsaved places, or insufficient metadata require user disambiguation; they do
not authorize choosing a global or recently active Studio.

## Play/Stop boundary

A transition is bound to:

```text
(studio_id, client_instance_id, document_epoch, transition_generation,
 play_request_id, expected_place_id, expected_game_id, transition_nonce)
```

The Edit-context plugin receives one bootstrap credential for the currently
authorized Play request. It creates one nonce-named temporary server Script
with fixed audited source. A caller cannot provide source, URL, instance path,
duration, transition identity, or cleanup target.

The Play server must prove Studio server context and matching document
identity, attach once, exchange the bootstrap credential for a derived server
credential, acknowledge an armed bounded watchdog, and acknowledge the exact
Stop command before calling `StudioTestService:EndTest()`.

The Edit-context runner accepts completion only after validating the returned
correlation, cleaning the exact temporary Script, and observing stable Edit
mode. Stale/replayed transitions, acknowledgements, credentials, server
identities, generations, and cross-place results fail closed.

## Disconnect, timeout, and replay policy

Edits, input, scripts, and lifecycle operations are never automatically
replayed. A call captures its target generation before waiting for the session
lock and fails if that generation changes.

A timed-out dispatched request creates an uncertain outcome and quarantines
the session. A same-generation late response or an authenticated plugin
settlement ledger can clear it. A dispatched job is not falsely reported
cancelled without downstream acknowledgement.

An incomplete Play transition enters recovery-only state after disconnect or
generation replacement. Bounded host and server watchdogs can request or
attempt Stop, but cannot report success. Normal operations remain fenced until
the original transition is proven complete.

## No host execution surface

The durable operation catalog does not expose:

- host process or shell execution;
- host filesystem access;
- arbitrary Luau evaluation;
- arbitrary URL fetching;
- model insertion;
- native keyboard, mouse, or Studio UI input.

Script updates are bounded compare-and-swap edits to an exact
`LuaSourceContainer` path in the targeted DataModel. Attribute changes require
expected prior state. Input can only fire an existing Scriptable
`InputBinding`. Studio paths are arrays of exact child-name segments, not code.

The plugin's loopback origin, endpoint allowlist, Play bridge source, and
Studio handlers are fixed. Enabling Studio's HTTP requests allows the plugin
to reach that fixed local broker; it does not permit caller-controlled URLs.

## Installation and update security

- The portable release contains placeholders, never rendered credentials.
- Installation generates fresh machine-local credentials with owner-only
  permissions.
- The v2 Codex table contains no bearer token; the stable local launcher reads
  private configuration.
- The installer owns only the separately named v2 support root, plugin, and
  Codex table. Existing v1 files remain outside its ownership.
- V2 never silently falls back to v1. An intentional v1 fallback is restricted
  to unsupported operations and must not be used to multiplex concurrent
  Studio tasks because v1 retains selection-based routing.
- Updates require an exact version tag, archive checksum, and internal
  manifest. They stage and validate before switching, retain a rollback
  version, and preserve the current install on failure.
- Upstream catalog import is local, explicit, digest-acknowledged, and
  fail-closed. Unknown operation families are never auto-published.
- The formal 25-tool parity ledger is fail-closed: partial/deferred P0 rows
  require a prerelease version, and an unsupported operation cannot invoke v1
  or another Studio implicitly.
- Repository and archive audits reject credentials, machine paths, private
  runtime material, and unsafe archive entries.

## Current limitations

- Native Apple Silicon macOS is the only supported platform. Intel and Rosetta
  are rejected before mutation.
- Broker session, correlation, and job state is in memory. After a clean
  restart, callers rediscover fresh Studio IDs.
- The local installation has one MCP principal. It is not multi-user identity
  federation.
- All operations use conservative per-session exclusivity. Shared reads would
  require a separate reentrancy audit.
- The native production Roblox proxy implementation is not reproduced or
  wrapped. V2 is an independent plugin based on documented Roblox APIs.
- Studio must allow HTTP requests for a document to register. Disabled access
  is a safe unregistered state.
- Screenshot and Scriptable `InputBinding` behavior remains subject to Roblox
  permissions and API availability.
