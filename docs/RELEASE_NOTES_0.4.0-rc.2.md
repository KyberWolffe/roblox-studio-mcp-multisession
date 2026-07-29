# Roblox Studio MCP v2 0.4.0-rc.2

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This corrected isolated Phase 2 candidate preserves the rc.1 workflow,
revision-protected multi-edit, recovery, and direct/job parity work while
fixing the failure that prevented its rendered Studio plugin from compiling.
The failed rc.1 commit and archive remain immutable and are explicitly
rejected; they are not overwritten or republished. The installed
`0.3.0-rc.4` integration and its checksum-manifested restore bundle remain
unchanged and are the required immediate rollback target.

## Native compilation correction

- The durable operation registry is rendered inside a private factory and
  exposes only a frozen `validateRequest`/`dispatch` facade.
- Optional services, numeric bounds, and protocol metadata are grouped into
  frozen tables, reducing the durable factory from more than 200 outer-scope
  locals to 121 while preserving every handler, schema, identity fence,
  per-session state object, and explicit route.
- The renderer adds a conservative 160-local static scope budget. The static
  check is defense in depth, not a substitute for compilation.
- A mandatory native Studio smoke gate extracts the exact sole `Main` source
  from the hashed `.rbxmx`, verifies the signed Studio executable, proves an
  empty Edit command-script context, and accepts only the reviewed early
  plugin-context assertion after successful whole-chunk compilation.
- A missing or mismatched receipt, invalid Studio signature, compiler/register
  error, unexpected plugin load, crash, or timeout leaves the candidate
  unqualified and mechanically blocks live validation.

## Read-only candidate-gate correction

- Direct and background-job calls now keep the public `*_v2` name through the
  client and authorization boundaries. Remote base names are used only to
  audit the catalog's exact bijection.
- Every operation requires one canonical explicit `studio_id`; caller-supplied
  nested targets, duplicate JSON keys, non-finite values, oversized payloads,
  mutation/Play/cancellation names, and catalog or module-origin drift fail
  before dispatch.
- Gate state revalidates the exact candidate plugin and catalog paths, hashes,
  modes, runtime port, authorization scope, and public/remote audit. It also
  binds and rechecks the complete extracted release manifest/tree and every
  runtime, secret, and upstream-config byte.
- Operational loading has no unqualified flag or capability-returning cleanup
  path. Native receipt revalidation is anchored to the fixed official Studio
  executable, while exact authenticated Stop remains available if proof,
  plugin, catalog, state, or extracted payload evidence is damaged.
- Preparation requires a caller-known release-manifest SHA before importing
  candidate code. A separate private cleanup identity binds runtime/secret
  hashes at preparation, and Start pins the exact authenticated broker
  instance; cleanup refuses substituted credentials, records, or brokers.

## Preserved Phase 2 behavior

The candidate retains bounded tree search/filter/pagination, script-name
search, literal cross-script grep, detailed instance inspection,
revision-protected multi-edit with exact recovery, and identity-bearing
direct/job receipts. Same-session work remains FIFO; different explicitly
targeted sessions remain isolated and concurrent.

Every operational v2 tool still requires explicit `studio_id`; discovery is
the only exception. There is no active/default Studio route and no silent v1
fallback. Full 25-tool parity is not claimed, and the exact ledger still
reports 12 partial or deferred P0 gaps.

This candidate is not installed or published. A matching native compile
receipt is required before any separately authorized live read-only gate, and
a further user gate is required before installation or mutation testing.
