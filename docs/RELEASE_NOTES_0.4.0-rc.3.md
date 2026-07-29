# Roblox Studio MCP v2 0.4.0-rc.3

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This isolated Phase 2 candidate preserves the rc.2 rendered-plugin,
candidate-gate, workflow, and recovery corrections while fixing the exact
native Studio identity expected by the mandatory compilation smoke gate. The
failed rc.1 and superseded rc.2 checkpoints and artifacts remain immutable;
neither is overwritten or republished. The installed `0.3.0-rc.4`
integration and its checksum-manifested restore bundle remain unchanged and
are the required immediate rollback target.

## Native identity correction

- The smoke gate now requires the official case-sensitive Studio
  bundle/signature identifier `com.Roblox.RobloxStudio` and exact Roblox
  signing team `2CFABCH843`.
- It still requires the caller-pinned Studio executable SHA-256 and verifies
  the app bundle with macOS `codesign --verify --deep --strict` before launch.
  The verification evaluates an Apple-anchored requirement containing the
  exact bundle identifier and Roblox leaf-certificate team OU; matching
  unsigned or self-signed metadata is insufficient.
- An invalid or unavailable signature, identifier/team mismatch, executable
  drift, running Studio process, compiler error, unexpected assertion, crash,
  timeout, or evidence drift fails closed.
- Native receipts use a new proof-format identity, preventing evidence from
  the earlier weaker signer contract from being accepted.
- No native proof is produced until the exact candidate package and sole
  `Main` source compile through a strictly verified Studio installation.

## Rendered-plugin compilation correction

- The durable operation registry remains inside a private factory exposing
  only a frozen `validateRequest`/`dispatch` facade.
- Immutable services, limits, and protocol metadata remain grouped, keeping
  the durable factory at 121 outer-scope locals and the rendered chunk at 120,
  with a conservative static budget below Luau's 200-local compiler limit.
- Static checks remain defense in depth. Official Luau compilation and the
  receipt-bound native Studio smoke gate are both required release evidence.

## Read-only candidate-gate correction

- Direct and background-job calls retain public `*_v2` names through client
  and authorization boundaries; remote base names are catalog-audit-only.
- Every operation requires one canonical explicit `studio_id`. There is no
  active/default target, mutation/Play scope, arbitrary Luau, or v1 fallback.
- Candidate loading binds the complete extracted release tree, caller-known
  release-manifest hash, catalog, runtime, secrets, configuration, package,
  source, native proof, and current Studio identity before capability use.
- Cleanup remains proof-independent but credential-, runtime-, and exact
  broker-instance-fenced; uncertain or substituted state fails closed.

## Preserved Phase 2 behavior

The candidate retains bounded tree search/filter/pagination, script-name
search, literal cross-script grep, detailed instance inspection,
revision-protected multi-edit with exact recovery, and identity-bearing
direct/job receipts. Same-session work remains FIFO; different explicitly
targeted sessions remain isolated and concurrent.

Every operational v2 tool still requires explicit `studio_id`; discovery is
the only exception. Full 25-tool parity is not claimed, and the exact ledger
continues to report 12 partial or deferred P0 gaps.

This candidate is not installed or published. A successful matching native
compile receipt is mandatory before any separately authorized live read-only
gate, and a further user gate is required before installation or mutation
testing.
