from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class GitHubWorkflowContractTests(unittest.TestCase):
    def test_workflows_are_arm64_only_and_actions_are_sha_pinned(self) -> None:
        workflows = [
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(),
            (ROOT / ".github" / "workflows" / "release.yml").read_text(),
        ]
        for source in workflows:
            self.assertIn("runs-on: macos-15", source)
            self.assertNotIn("macos-14", source)
            self.assertNotIn("macos-latest", source)
            for action in re.findall(r"uses:\s*([^\s#]+)", source):
                self.assertRegex(
                    action,
                    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$",
                )

    def test_release_publish_is_commit_pinned_and_refetches_tag(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "commit_sha: ${{ steps.version.outputs.commit_sha }}",
            source,
        )
        self.assertIn(
            "ref: ${{ needs.build.outputs.commit_sha }}",
            source,
        )
        self.assertNotIn(
            "ref: ${{ needs.build.outputs.tag }}",
            source,
        )
        self.assertIn(
            '"refs/tags/$RELEASE_TAG:$verify_ref"',
            source,
        )
        self.assertIn(
            'git rev-parse "${verify_ref}^{}"',
            source,
        )
        self.assertIn(
            "RELEASE_POLICY_ATTESTED: "
            "${{ vars.RELEASE_POLICY_ATTESTED }}",
            source,
        )
        self.assertIn(
            '"immutable-releases+protected-v-tags-v1"',
            source,
        )
        self.assertIn("--draft", source)
        self.assertIn("--target \"$RELEASE_COMMIT_SHA\"", source)
        self.assertIn("--json isImmutable", source)
        self.assertIn("gh release verify \"$RELEASE_TAG\"", source)
        compare = source.index(
            '[[ "$remote_tag_commit" != "$RELEASE_COMMIT_SHA" ]]'
        )
        draft = source.index("gh release create")
        publish = source.index('gh release edit "$RELEASE_TAG"')
        self.assertLess(compare, draft)
        self.assertLess(draft, publish)
        final_compare = source.rindex(
            '[[ "$remote_tag_commit" != "$RELEASE_COMMIT_SHA" ]]'
        )
        self.assertGreater(final_compare, draft)
        self.assertNotIn("gh ", source[final_compare:publish])

    def test_incomplete_p0_parity_is_checked_with_exact_tag(self) -> None:
        source = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'validate_capability_parity.py --tag "$RELEASE_TAG"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
