import datetime
import io
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import main as semantic_main


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, tzinfo=tz)


class SemanticVersionerPreviewTests(unittest.TestCase):
    def run_git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit_file(self, repo: Path, filename: str, content: str, message: str) -> str:
        (repo / filename).write_text(content)
        self.run_git(repo, "add", filename)
        self.run_git(repo, "commit", "-m", message)
        return self.run_git(repo, "rev-parse", "HEAD")

    def init_repo(self) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        repo = Path(tmpdir.name)
        self.run_git(repo, "init", "-b", "main")
        self.run_git(repo, "config", "user.name", "Test User")
        self.run_git(repo, "config", "user.email", "test@example.com")
        self.commit_file(repo, "README.md", "initial\n", "chore: initial")
        return repo

    def build_versioner(self, repo: Path, main_branch: str = "main") -> semantic_main.SemanticVersioner:
        versioner = semantic_main.SemanticVersioner(
            str(repo),
            True,
            main_branch,
            False,
        )
        self.assertTrue(versioner.initialize())
        return versioner

    def parse_github_output(self, path: Path) -> dict[str, str]:
        outputs: dict[str, str] = {}
        lines = path.read_text().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line:
                name, delimiter = line.split("<<", 1)
                i += 1
                value_lines = []
                while i < len(lines) and lines[i] != delimiter:
                    value_lines.append(lines[i])
                    i += 1
                outputs[name] = "\n".join(value_lines) + "\n"
            else:
                name, value = line.split("=", 1)
                outputs[name] = value
            i += 1
        return outputs

    def test_main_preview_cli_outputs_and_file(self):
        repo = self.init_repo()
        self.run_git(repo, "tag", "v1.2.3")
        self.commit_file(
            repo,
            "feature.txt",
            "feature\n",
            "feat(api): add endpoint\n\nCHANGELOG: Added API endpoint",
        )

        github_output = repo / "github_output.txt"
        preview_file = repo / "preview.md"
        env = os.environ.copy()
        env["GITHUB_OUTPUT"] = str(github_output)

        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
                with redirect_stdout(stdout):
                    exit_code = semantic_main.main(
                        [
                            "--repository",
                            str(repo),
                            "--no-fetch",
                            "--preview-changelog",
                            "--print-changelog",
                            "--preview-output-file",
                            str(preview_file),
                        ]
                    )

        expected_markdown = textwrap.dedent(
            """\
            ## 1.3.0 (2026-01-02 03:04)

            ### Api

            #### FEATURE
            - Added API endpoint
            """
        )
        self.assertEqual(exit_code, 0)
        outputs = self.parse_github_output(github_output)
        self.assertEqual(outputs["previous-version"], "v1.2.3")
        self.assertEqual(outputs["new-version"], "v1.3.0")
        self.assertEqual(outputs["rendered-changelog"], expected_markdown)
        self.assertEqual(preview_file.read_text(), expected_markdown)
        self.assertIn(expected_markdown, stdout.getvalue())

    def test_dev_preview_with_semantic_dev_versions(self):
        repo = self.init_repo()
        self.run_git(repo, "tag", "v1.2.3")
        self.run_git(repo, "checkout", "-b", "develop")
        first_dev_commit = self.commit_file(
            repo,
            "dev.txt",
            "feature\n",
            "feat(ui): add dashboard\n\nCHANGELOG: Added dashboard",
        )
        self.run_git(repo, "tag", "v1.3.0-dev.0.0.1", first_dev_commit)
        self.commit_file(
            repo,
            "fix.txt",
            "fix\n",
            "fix(ui): align spacing\n\nCHANGELOG: Fixed dashboard spacing",
        )

        with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
            preview = self.build_versioner(repo).get_dev_preview(
                dev_branch="develop",
                dev_suffix="dev",
                dev_version_style=semantic_main.DevVersionStyle.SEMANTIC,
            )

        self.assertIsNotNone(preview)
        self.assertEqual(str(preview.previous_version), "1.3.0-dev.0.0.1")
        self.assertEqual(str(preview.new_version), "1.3.0-dev.0.0.2")
        self.assertIn("### Ui", preview.rendered_changelog_markdown)
        self.assertIn("- Fixed dashboard spacing", preview.rendered_changelog_markdown)

    def test_dev_preview_target_ref_increments_existing_dev_prerelease(self):
        """A preview against a PR merge ref must increment the existing latest
        dev prerelease bits, not recompute them from scratch. Given latest dev
        2.3.0-dev.0.1.2 and a patch-level PR, the preview must return
        2.3.0-dev.0.1.3 and equal the non-preview computation for the same
        target commit."""
        repo = self.init_repo()
        self.run_git(repo, "tag", "v2.2.0")
        self.run_git(repo, "checkout", "-b", "develop")
        # A feat commit means the dev branch is a minor bump over main (2.3.0),
        # matching the base of the latest dev tag below.
        dev_base_commit = self.commit_file(
            repo,
            "dashboard.txt",
            "feature\n",
            "feat(ui): add dashboard\n\nCHANGELOG: Added dashboard",
        )
        self.run_git(repo, "tag", "v2.3.0-dev.0.1.2", dev_base_commit)
        # A patch-level PR on top of the latest dev tag.
        patch_commit = self.commit_file(
            repo,
            "spacing.txt",
            "fix\n",
            "fix(ui): align spacing\n\nCHANGELOG: Fixed dashboard spacing",
        )
        self.run_git(repo, "update-ref", "refs/pull/1/merge", patch_commit)

        versioner = self.build_versioner(repo)
        with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
            preview = versioner.get_dev_preview(
                dev_branch="develop",
                dev_suffix="dev",
                dev_version_style=semantic_main.DevVersionStyle.SEMANTIC,
                target_ref="refs/pull/1/merge",
            )
            # The non-preview computation walks the same routine via the dev
            # branch head, which is the same commit as the PR merge ref here.
            non_preview = versioner.get_dev_preview(
                dev_branch="develop",
                dev_suffix="dev",
                dev_version_style=semantic_main.DevVersionStyle.SEMANTIC,
            )

        self.assertIsNotNone(preview)
        self.assertEqual(preview.end_commit.hexsha, patch_commit)
        self.assertEqual(str(preview.previous_version), "2.3.0-dev.0.1.2")
        self.assertEqual(str(preview.new_version), "2.3.0-dev.0.1.3")
        # Preview output must match what an actual merge would produce.
        self.assertEqual(
            str(preview.new_version), str(non_preview.new_version)
        )

    def test_arbitrary_target_ref_preview(self):
        repo = self.init_repo()
        self.run_git(repo, "tag", "v1.0.0")
        self.run_git(repo, "checkout", "-b", "develop")
        self.run_git(repo, "checkout", "-b", "feature/preview")
        self.commit_file(
            repo,
            "preview.txt",
            "preview\n",
            "feat(core): preview merge result\n\nCHANGELOG: Added merge preview",
        )
        self.run_git(repo, "checkout", "develop")
        self.run_git(repo, "checkout", "-b", "pr-merge")
        self.run_git(repo, "merge", "--no-ff", "feature/preview", "-m", "Merge feature preview")
        merge_commit = self.run_git(repo, "rev-parse", "HEAD")
        self.run_git(repo, "update-ref", "refs/pull/1/merge", merge_commit)
        self.run_git(repo, "checkout", "develop")

        with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
            preview = self.build_versioner(repo).get_dev_preview(
                dev_branch="develop",
                dev_suffix="dev",
                dev_version_style=semantic_main.DevVersionStyle.INCREMENTING,
                target_ref="refs/pull/1/merge",
            )

        self.assertIsNotNone(preview)
        self.assertEqual(preview.end_commit.hexsha, merge_commit)
        self.assertEqual(str(preview.new_version), "1.1.0-dev.1")
        self.assertIn("- Added merge preview", preview.rendered_changelog_markdown)

    def test_rendered_changelog_matches_existing_format(self):
        repo = self.init_repo()
        self.run_git(repo, "tag", "v2.0.0")
        self.commit_file(
            repo,
            "breaking.txt",
            "breaking\n",
            "feat(api/client)!: replace transport\n\nCHANGELOG: Replaced HTTP transport",
        )

        with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
            preview = self.build_versioner(repo).get_main_preview(
                changelog_message="Release summary",
            )

        self.assertEqual(
            preview.rendered_changelog_markdown,
            textwrap.dedent(
                """\
                ## 3.0.0 (2026-01-02 03:04)

                ### Api

                #### FEATURE
                - Replaced HTTP transport (BREAKING CHANGE)

                ### Client

                #### FEATURE
                - Replaced HTTP transport (BREAKING CHANGE)

                ### Other

                #### OTHER
                - Release summary
                """
            ),
        )

    def test_preview_mode_does_not_mutate_git_state(self):
        repo = self.init_repo()
        self.run_git(repo, "tag", "v1.0.0")
        self.commit_file(
            repo,
            "preview.txt",
            "preview\n",
            "fix: prepare preview\n\nCHANGELOG: Prepared preview",
        )

        head_before = self.run_git(repo, "rev-parse", "HEAD")
        branch_before = self.run_git(repo, "branch", "--show-current")
        tags_before = self.run_git(repo, "tag", "--list")
        status_before = self.run_git(repo, "status", "--porcelain")

        temp_output = tempfile.NamedTemporaryFile(delete=False)
        temp_output.close()
        self.addCleanup(lambda: os.path.exists(temp_output.name) and os.unlink(temp_output.name))
        env = os.environ.copy()
        env["GITHUB_OUTPUT"] = temp_output.name

        with mock.patch.dict(os.environ, env, clear=False):
            exit_code = semantic_main.main(
                [
                    "--repository",
                    str(repo),
                    "--no-fetch",
                    "--preview-changelog",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.run_git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(self.run_git(repo, "branch", "--show-current"), branch_before)
        self.assertEqual(self.run_git(repo, "tag", "--list"), tags_before)
        self.assertEqual(self.run_git(repo, "status", "--porcelain"), status_before)

    def test_preview_succeeds_without_push_credentials(self):
        """Preview mode must not attempt any push, even when the action forces
        PUSH=1 and the remote is unreachable / read-only (mirrors a
        pull_request_target job with a read-only token)."""
        repo = self.init_repo()
        self.run_git(repo, "tag", "v1.0.0")
        self.commit_file(
            repo,
            "feature.txt",
            "feature\n",
            "feat(api): add endpoint\n\nCHANGELOG: Added API endpoint",
        )

        # An unreachable remote: any push attempt would fail loudly, so a
        # successful run proves no push (or tag deletion) was attempted.
        self.run_git(
            repo,
            "remote",
            "add",
            "origin",
            "https://invalid.invalid/owner/repo.git",
        )

        tags_before = self.run_git(repo, "tag", "--list")

        github_output = repo / "github_output.txt"
        env = os.environ.copy()
        env["GITHUB_OUTPUT"] = str(github_output)
        # Replicate action.yml, which forces PUSH=1 for the non-preview path.
        env["PUSH"] = "1"

        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(semantic_main.datetime, "datetime", FixedDateTime):
                exit_code = semantic_main.main(
                    [
                        "--repository",
                        str(repo),
                        "--no-fetch",
                        "--preview-changelog",
                    ]
                )

        self.assertEqual(exit_code, 0)
        outputs = self.parse_github_output(github_output)
        self.assertEqual(outputs["previous-version"], "v1.0.0")
        self.assertEqual(outputs["new-version"], "v1.1.0")
        self.assertIn("- Added API endpoint", outputs["rendered-changelog"])
        # No new tags were created locally and the run did not error on push.
        self.assertEqual(self.run_git(repo, "tag", "--list"), tags_before)

    def test_mutating_methods_refuse_in_preview_mode(self):
        """The centralised guard must block every write path in preview mode."""
        repo = self.init_repo()
        versioner = semantic_main.SemanticVersioner(
            str(repo),
            True,
            "main",
            False,
            preview=True,
        )
        self.assertTrue(versioner.initialize())

        with self.assertRaises(RuntimeError):
            versioner.push_tags()

        with self.assertRaises(RuntimeError):
            versioner.write_changelog("main", str(repo / "CHANGELOG.md"), "## 1.0.0\n")

        with self.assertRaises(RuntimeError):
            versioner._add_version_tags_to_commit(
                versioner._main_head_commit,
                semantic_main.semver.Version.parse("1.0.0"),
            )

        # The guard must not leak into normal (non-preview) operation.
        self.assertFalse(
            semantic_main.SemanticVersioner(str(repo), True, "main", False)._preview
        )


if __name__ == "__main__":
    unittest.main()
