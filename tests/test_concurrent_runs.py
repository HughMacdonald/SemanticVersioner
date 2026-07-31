import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main as semantic_main


class ConcurrentRunTests(unittest.TestCase):
    """Regression tests for concurrent / duplicate runs of the action.

    A local bare repository stands in for GitHub as the "remote", so no network
    access is required.
    """

    def run_git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def tmpdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def init_bare_remote(self) -> Path:
        remote = self.tmpdir() / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(remote)],
            check=True,
            capture_output=True,
        )
        return remote

    def commit_file(self, repo: Path, filename: str, content: str, message: str) -> str:
        (repo / filename).write_text(content)
        self.run_git(repo, "add", filename)
        self.run_git(repo, "commit", "-m", message)
        return self.run_git(repo, "rev-parse", "HEAD")

    def clone(self, remote: Path) -> Path:
        work = self.tmpdir() / f"clone-{os.urandom(4).hex()}"
        subprocess.run(
            ["git", "clone", str(remote), str(work)],
            check=True,
            capture_output=True,
        )
        self.run_git(work, "config", "user.name", "Test User")
        self.run_git(work, "config", "user.email", "test@example.com")
        return work

    def remote_tag_sha(self, remote: Path, tag: str) -> str:
        return self.run_git(remote, "rev-parse", f"refs/tags/{tag}")

    def remote_has_tag(self, remote: Path, tag: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
            cwd=remote,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def seed_main_baseline(self, remote: Path) -> str:
        """Seed the remote with v1.0.0 plus a feat commit that is the triggering
        sha for the two racing runs. Returns that triggering sha."""
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        trigger = self.commit_file(
            seed,
            "feature.txt",
            "feature\n",
            "feat: add feature\n\nCHANGELOG: Added feature",
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        return trigger

    def run_main(self, work: Path, *extra: str, commit_date: str = None) -> int:
        github_output = work / "github_output.txt"
        env = os.environ.copy()
        env["GITHUB_OUTPUT"] = str(github_output)
        if commit_date is not None:
            # Force a specific commit timestamp so two racing runs produce
            # genuinely divergent changelog commits (as in the real incident,
            # where the two runs were ~30s apart), rather than an identical,
            # content-addressed commit that would fast-forward cleanly.
            env["GIT_AUTHOR_DATE"] = commit_date
            env["GIT_COMMITTER_DATE"] = commit_date
        with mock.patch.dict(os.environ, env, clear=False):
            return semantic_main.main(
                [
                    "--repository",
                    str(work),
                    "--no-fetch",
                    "--main-branch",
                    "main",
                    "--changelog-file",
                    str(work / "CHANGELOG.md"),
                    "--push",
                    *extra,
                ]
            )

    def test_incident_second_run_branch_push_rejected(self):
        """Reproduce the production incident: two runs from the same starting sha
        where the first pushes successfully and the second's branch push is
        rejected. The second run must exit non-zero and must not touch the tag
        the first run created."""
        remote = self.init_bare_remote()
        trigger = self.seed_main_baseline(remote)

        run_a = self.clone(remote)
        run_b = self.clone(remote)

        # Both runs are checked out at the same triggering sha.
        self.assertEqual(self.run_git(run_a, "rev-parse", "HEAD"), trigger)
        self.assertEqual(self.run_git(run_b, "rev-parse", "HEAD"), trigger)

        # Run A succeeds and publishes v1.1.0 on its own changelog commit.
        exit_a = self.run_main(run_a, commit_date="2026-01-01T17:18:38")
        self.assertEqual(exit_a, 0)
        run_a_commit = self.run_git(remote, "rev-parse", "refs/heads/main")
        self.assertTrue(self.remote_has_tag(remote, "v1.1.0"))
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), run_a_commit)

        # Run B is still at the old sha and has not fetched Run A's push.
        exit_b = self.run_main(run_b, commit_date="2026-01-01T17:19:10")

        # Its branch push is rejected, so it must fail...
        self.assertNotEqual(exit_b, 0)
        # ...the remote branch and tag are exactly as Run A left them...
        self.assertEqual(
            self.run_git(remote, "rev-parse", "refs/heads/main"), run_a_commit
        )
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), run_a_commit)
        # ...and Run B created no local tag either (nothing to clean up).
        self.assertNotIn(
            "v1.1.0", self.run_git(run_b, "tag", "--list").splitlines()
        )

    def test_existing_remote_tag_on_different_commit_fails(self):
        """A remote tag that already exists on a different commit must never be
        moved; the run fails naming both shas."""
        remote = self.init_bare_remote()
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        commit_one = self.commit_file(
            seed, "a.txt", "a\n", "feat: one\n\nCHANGELOG: one"
        )
        commit_two = self.commit_file(
            seed, "b.txt", "b\n", "feat: two\n\nCHANGELOG: two"
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        # Publish v1.1.0 on commit_two already.
        self.run_git(seed, "tag", "v1.1.0", commit_two)
        self.run_git(seed, "push", "origin", "v1.1.0")

        versioner = semantic_main.SemanticVersioner(
            str(seed), True, "main", False
        )
        self.assertTrue(versioner.initialize())

        with self.assertRaises(semantic_main.TagConflictError) as ctx:
            versioner._add_version_tags_to_commit(
                versioner._repository.commit(commit_one),
                semantic_main.semver.Version.parse("1.1.0"),
                push=True,
            )

        message = str(ctx.exception)
        self.assertIn(commit_one, message)
        self.assertIn(commit_two, message)
        # The remote tag is untouched.
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), commit_two)

    def test_existing_remote_tag_on_same_commit_is_noop(self):
        """Re-running against a tag that already points at the intended commit is
        an idempotent success."""
        remote = self.init_bare_remote()
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        commit = self.commit_file(
            seed, "a.txt", "a\n", "feat: one\n\nCHANGELOG: one"
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        self.run_git(seed, "tag", "v1.1.0", commit)
        self.run_git(seed, "push", "origin", "v1.1.0")

        versioner = semantic_main.SemanticVersioner(
            str(seed), True, "main", False
        )
        self.assertTrue(versioner.initialize())

        result = versioner._add_version_tags_to_commit(
            versioner._repository.commit(commit),
            semantic_main.semver.Version.parse("1.1.0"),
            push=True,
        )
        self.assertTrue(result)
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), commit)

    def test_allow_tag_move_permits_force_move(self):
        """With allow_tag_move enabled, an existing remote tag may be moved."""
        remote = self.init_bare_remote()
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        commit_one = self.commit_file(
            seed, "a.txt", "a\n", "feat: one\n\nCHANGELOG: one"
        )
        commit_two = self.commit_file(
            seed, "b.txt", "b\n", "feat: two\n\nCHANGELOG: two"
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        self.run_git(seed, "tag", "v1.1.0", commit_one)
        self.run_git(seed, "push", "origin", "v1.1.0")

        versioner = semantic_main.SemanticVersioner(
            str(seed), True, "main", False, allow_tag_move=True
        )
        self.assertTrue(versioner.initialize())

        result = versioner._add_version_tags_to_commit(
            versioner._repository.commit(commit_two),
            semantic_main.semver.Version.parse("1.1.0"),
            push=True,
        )
        self.assertTrue(result)
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), commit_two)

    def test_rolling_alias_tags_move_without_allow_tag_move(self):
        """With include_shorter_versions, the rolling alias tags (v1, v1.2) must
        advance to each new release automatically, without allow_tag_move."""
        remote = self.init_bare_remote()
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        old_commit = self.commit_file(
            seed, "a.txt", "a\n", "feat: one\n\nCHANGELOG: one"
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        # A previous release already published the rolling alias v1 on old_commit.
        self.run_git(seed, "tag", "v1", old_commit)
        self.run_git(seed, "push", "origin", "v1")
        new_commit = self.commit_file(
            seed, "b.txt", "b\n", "feat: two\n\nCHANGELOG: two"
        )
        self.run_git(seed, "push", "origin", "main")

        # include_shorter_versions=True, allow_tag_move left at its default False.
        versioner = semantic_main.SemanticVersioner(
            str(seed), True, "main", True
        )
        self.assertTrue(versioner.initialize())

        result = versioner._add_version_tags_to_commit(
            versioner._repository.commit(new_commit),
            semantic_main.semver.Version.parse("1.1.0"),
            push=True,
        )
        self.assertTrue(result)
        # The unique release tag is created on the new commit...
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), new_commit)
        # ...and the rolling aliases advanced to it, with no allow_tag_move.
        self.assertEqual(self.remote_tag_sha(remote, "v1.1"), new_commit)
        self.assertEqual(self.remote_tag_sha(remote, "v1"), new_commit)

    def test_release_tag_protected_even_with_shorter_versions(self):
        """Even with include_shorter_versions, a conflicting unique release tag is
        still protected: the run fails and never repoints it."""
        remote = self.init_bare_remote()
        seed = self.clone(remote)
        self.commit_file(seed, "README.md", "initial\n", "chore: initial")
        self.run_git(seed, "tag", "v1.0.0")
        commit_one = self.commit_file(
            seed, "a.txt", "a\n", "feat: one\n\nCHANGELOG: one"
        )
        commit_two = self.commit_file(
            seed, "b.txt", "b\n", "feat: two\n\nCHANGELOG: two"
        )
        self.run_git(seed, "push", "origin", "main")
        self.run_git(seed, "push", "origin", "v1.0.0")
        # The unique release tag already exists on a different commit.
        self.run_git(seed, "tag", "v1.1.0", commit_one)
        self.run_git(seed, "push", "origin", "v1.1.0")

        versioner = semantic_main.SemanticVersioner(
            str(seed), True, "main", True
        )
        self.assertTrue(versioner.initialize())

        with self.assertRaises(semantic_main.TagConflictError) as ctx:
            versioner._add_version_tags_to_commit(
                versioner._repository.commit(commit_two),
                semantic_main.semver.Version.parse("1.1.0"),
                push=True,
            )

        message = str(ctx.exception)
        self.assertIn(commit_one, message)
        self.assertIn(commit_two, message)
        self.assertEqual(self.remote_tag_sha(remote, "v1.1.0"), commit_one)

    def test_dry_run_performs_no_mutations(self):
        """dry-run must not commit, tag, or push anything."""
        remote = self.init_bare_remote()
        self.seed_main_baseline(remote)
        work = self.clone(remote)

        remote_main_before = self.run_git(remote, "rev-parse", "refs/heads/main")
        remote_tags_before = self.run_git(remote, "tag", "--list")
        local_head_before = self.run_git(work, "rev-parse", "HEAD")
        local_tags_before = self.run_git(work, "tag", "--list")

        exit_code = self.run_main(work, "--dry-run")

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.run_git(remote, "rev-parse", "refs/heads/main"),
            remote_main_before,
        )
        self.assertEqual(self.run_git(remote, "tag", "--list"), remote_tags_before)
        self.assertEqual(self.run_git(work, "rev-parse", "HEAD"), local_head_before)
        self.assertEqual(self.run_git(work, "tag", "--list"), local_tags_before)

    def test_stale_trigger_fail_exits_nonzero_without_mutating(self):
        """When the branch tip has advanced beyond the checked-out sha and
        stale-trigger=fail, the run exits non-zero and mutates nothing."""
        remote = self.init_bare_remote()
        self.seed_main_baseline(remote)
        work = self.clone(remote)

        # Advance the remote branch tip via another clone (a concurrent run).
        other = self.clone(remote)
        self.commit_file(
            other, "c.txt", "c\n", "feat: concurrent\n\nCHANGELOG: concurrent"
        )
        self.run_git(other, "push", "origin", "main")

        remote_tags_before = self.run_git(remote, "tag", "--list")

        exit_code = self.run_main(work, "--stale-trigger", "fail")
        self.assertNotEqual(exit_code, 0)
        self.assertEqual(self.run_git(remote, "tag", "--list"), remote_tags_before)

    def test_stale_trigger_skip_exits_zero_without_mutating(self):
        remote = self.init_bare_remote()
        self.seed_main_baseline(remote)
        work = self.clone(remote)

        other = self.clone(remote)
        self.commit_file(
            other, "c.txt", "c\n", "feat: concurrent\n\nCHANGELOG: concurrent"
        )
        self.run_git(other, "push", "origin", "main")

        remote_tags_before = self.run_git(remote, "tag", "--list")

        exit_code = self.run_main(work, "--stale-trigger", "skip")
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.run_git(remote, "tag", "--list"), remote_tags_before)


if __name__ == "__main__":
    unittest.main()
