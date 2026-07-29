# Roblox Studio MCP v2 0.4.0-rc.4

<!-- experimental-prerelease: true -->
<!-- capability-parity: incomplete -->
<!-- global-v1-fallback: forbidden -->

This isolated Phase 2 candidate supersedes rc.3 because rc.3 treated
full-bundle deep/strict signature verification as a hard functional
qualification prerequisite. Rc.1 through rc.3 remain immutable historical
checkpoints. The installed `0.3.0-rc.4` integration and its restore bundle
remain unchanged and are the required immediate rollback target.

## Proportional native gate

- The hard identity check binds the exact Roblox Studio main-executable
  SHA-256, Info.plist version/build and bundle executable, and narrow
  Apple/Roblox signing identity and CDHash.
- Full-bundle `codesign --verify --deep --strict` is retained as optional
  diagnostic/provenance evidence, not a functional blocker.
- The exact rendered candidate package and sole `Main` source are hashed, and
  that source must compile without register/compiler errors.
- Compile-only evidence does not claim plugin loading. A linked future gate
  must load the actual candidate plugin, observe expected registration and
  clean candidate logs, and complete bounded read-only checks against explicit
  `studio_id` targets.

Every operational v2 tool continues to require an explicit `studio_id`;
discovery is the sole exception. No active/default Studio routing, silent v1
fallback, mutation, Play/Stop, save, or publish is authorized by this
candidate qualification.

This candidate is not installed or published. Any future live read-only gate
or installation remains a separate user-authorized step.
