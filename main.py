import argparse
import datetime
import itertools
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Tuple

import git
import semver

log = logging.getLogger()
log.setLevel(logging.DEBUG)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(
    logging.Formatter("%(asctime)s : %(levelname)s : %(message)s")
)
log.addHandler(stream_handler)


class SemanticVersionerError(RuntimeError):
    """Base class for fatal, user-facing errors raised by the versioner."""


class BranchPushError(SemanticVersionerError):
    """A push of a branch or tag to the remote was rejected."""


class TagConflictError(SemanticVersionerError):
    """A remote tag already exists on a different commit and must not be moved."""


class CommitType(IntEnum):
    OTHER = 0
    FIX = 1
    FEATURE = 2


class VersionUpdateEnum(IntEnum):
    PATCH = 0
    MINOR = 1
    MAJOR = 2


class DevVersionStyle(IntEnum):
    INCREMENTING = 0
    SEMANTIC = 1


@dataclass
class VersionUpdateRegex:
    regex: re.Pattern
    version_update: VersionUpdateEnum
    commit_type: CommitType


@dataclass
class VersionPreview:
    previous_version: semver.Version
    new_version: semver.Version
    start_commit: Optional[git.Commit]
    end_commit: git.Commit
    changelog: dict[Optional[str], dict[CommitType, list[str]]]
    rendered_changelog_markdown: str


class SemanticVersioner:
    _changelog_regex = re.compile(r"^CHANGELOG:\s*(?P<message>.*)$")

    # Regular expressions to be run on commit messages to determine which
    # version part to update
    _version_update_regexes: list[VersionUpdateRegex] = [
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # anything(anything):
            regex=re.compile(r"^\w+(\((?P<scopes>.*)\))?:", re.I),
            version_update=VersionUpdateEnum.PATCH,
            commit_type=CommitType.OTHER,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # fix(anything):
            # fix:
            regex=re.compile(r"^fix(\((?P<scopes>.*)\))?:", re.I),
            version_update=VersionUpdateEnum.PATCH,
            commit_type=CommitType.FIX,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything):
            # feat:
            regex=re.compile(r"^feat(\((?P<scopes>.*)\))?:", re.I),
            version_update=VersionUpdateEnum.MINOR,
            commit_type=CommitType.FEATURE,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything)!:
            # feat!:
            # fix(anything)!:
            # fix!:
            regex=re.compile(r"^fix(\((?P<scopes>.*)\))?!:", re.I),
            version_update=VersionUpdateEnum.MAJOR,
            commit_type=CommitType.FIX,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything)!:
            # feat!:
            # fix(anything)!:
            # fix!:
            regex=re.compile(r"^feat(\((?P<scopes>.*)\))?!:", re.I),
            version_update=VersionUpdateEnum.MAJOR,
            commit_type=CommitType.FEATURE,
        ),
        VersionUpdateRegex(
            # Commit message starting with (cast insensitive):
            # breaking change:
            regex=re.compile(r"^breaking\s+change:", re.I),
            version_update=VersionUpdateEnum.MAJOR,
            commit_type=CommitType.OTHER,
        ),
    ]

    # The string to be used as the prefix for all versions. The tags are
    # expected to be this, followed by the string representation of the
    # semver.Version object
    _version_prefix = "v"

    # Push flags that indicate the remote refused (or partially refused) a push.
    # With --porcelain, GitPython reports these on the returned PushInfo objects,
    # which is far more reliable than scraping git's stderr text.
    _push_error_flags = (
        git.PushInfo.ERROR
        | git.PushInfo.REJECTED
        | git.PushInfo.REMOTE_REJECTED
        | git.PushInfo.REMOTE_FAILURE
    )

    def __init__(
        self,
        repository_path: str,
        no_fetch: bool,
        main_branch: str,
        include_shorter_versions: bool,
        preview: bool = False,
        allow_tag_move: bool = False,
    ):
        self._repository = git.Repo(repository_path)
        self._no_fetch = no_fetch
        self._main_branch = main_branch
        self._main_head_commit: Optional[git.Commit] = None
        self._include_shorter_versions = include_shorter_versions
        # When True, the versioner is in read-only preview mode and must never
        # mutate the local repository or the remote (no commits, tags or pushes).
        self._preview = preview
        # When True, the unique full release tag may be force-moved if it already
        # exists on the remote at a different commit. When False (the default) the
        # release tag is left untouched and the run fails, so a concurrent or
        # duplicate run can never silently repoint a published release tag. This
        # does not affect the rolling alias tags from include_shorter_versions,
        # which always advance to each new release.
        self._allow_tag_move = allow_tag_move

    def _ensure_not_preview(self, operation: str) -> None:
        """
        Guard against any mutating operation while in preview mode.
        :param operation: A human-readable description of the attempted mutation
        :raises RuntimeError: If the versioner is in preview mode
        """
        if self._preview:
            raise RuntimeError(
                f"Refusing to {operation} in preview mode (preview is read-only)"
            )

    def initialize(self) -> bool:
        """
        Initialize the object
        :return: Whether the initialization was successful
        """
        if not self._no_fetch:
            is_shallow = self._repository.git.rev_parse("--is-shallow-repository") == "true"
            self._repository.remote().fetch(tags=True, unshallow=is_shallow)
        self._main_head_commit = self._get_branch_head_commit(self._main_branch)
        if not self._main_head_commit:
            log.error(f"Branch not found: {self._main_branch}")
            branch_names = [branch.name for branch in self._repository.branches]
            log.debug(f"Available branches: {', '.join(branch_names)}")
            return False

        return True

    def write_changelog(
        self,
        branch_name: str,
        changelog_file: str,
        rendered_changelog_markdown: str,
    ) -> git.Commit:
        """
        Write the changelog to the specified file
        :param branch_name: The name of the branch to write the changelog for
        :param changelog_file: The file to write the changelog to
        :param rendered_changelog_markdown: The rendered changelog markdown to write
        :return: The commit object for the new commit
        """
        self._ensure_not_preview("write the changelog")

        existing_changelog = None

        log.info(f"Writing changelog file to {changelog_file}")

        if os.path.isfile(changelog_file):
            with open(changelog_file, "r") as fd:
                existing_changelog = fd.read()

        with open(changelog_file, "w") as fd:
            fd.write(rendered_changelog_markdown)
            if existing_changelog:
                fd.write("\n")
                fd.write(existing_changelog)

        log.info("Committing changelog")
        self._repository.git.checkout(branch_name)
        self._repository.index.add([changelog_file])
        version_header = rendered_changelog_markdown.splitlines()[0]
        version = version_header.split()[1]
        new_commit = self._repository.index.commit(f"Update changelog for {version}")

        self._push_branch(branch_name)

        return new_commit

    def _push_branch(self, branch_name: str) -> None:
        """
        Push a branch to origin and fail loudly if the remote rejected it.

        A rejected branch push (for example a non-fast-forward caused by a
        concurrent run that already advanced the branch) must be fatal: the
        commit does not exist on the remote afterwards, so anything that follows
        (in particular tagging that commit) would be invalid.
        :param branch_name: The branch to push
        :raises BranchPushError: If the remote rejected the push
        """
        self._ensure_not_preview("push a branch")
        log.info(f"Pushing branch '{branch_name}' to origin")
        self._push_ref(branch_name, f"branch '{branch_name}'")

    def _push_ref(self, refspec: str, ref_description: str) -> None:
        """
        Push a single refspec to origin and fail loudly if it was rejected.

        Handles both ways GitPython can surface a rejection: a raised
        GitCommandError, or PushInfo objects carrying error flags.
        :param refspec: The refspec to push (e.g. a branch name or
        ``refs/tags/<tag>``, optionally prefixed with ``+`` to force)
        :param ref_description: Human-readable description of what is pushed,
        included verbatim in any error so the rejected ref is named
        :raises BranchPushError: If the remote rejected the push
        """
        origin = self._repository.remote(name="origin")
        try:
            push_info = origin.push(refspec)
        except git.exc.GitCommandError as exc:
            message = f"Failed to push {ref_description} to origin: {exc}"
            log.error(message)
            raise BranchPushError(message) from exc
        self._raise_for_push_errors(push_info, ref_description)

    @classmethod
    def _raise_for_push_errors(cls, push_info_list, ref_description: str) -> None:
        """
        Inspect the PushInfo results of a push and raise if any ref was rejected.

        GitPython emits a misleading ``"Error lines received while fetching"``
        warning on a rejected *push*; rather than relying on that text we check
        the structured ERROR/REJECTED/REMOTE_REJECTED flags on each PushInfo.
        :param push_info_list: The result of ``Remote.push``
        :param ref_description: Human-readable description of what was pushed,
        included verbatim in the error so the rejected ref is named
        :raises BranchPushError: If any pushed ref reported an error flag
        """
        errored = [
            info
            for info in push_info_list
            if info.flags & cls._push_error_flags
        ]
        if not errored:
            return

        summaries = "; ".join(
            (info.summary or "").strip() for info in errored if info.summary
        )
        message = f"Failed to push {ref_description} to origin: rejected by remote"
        if summaries:
            message = f"{message} ({summaries})"
        log.error(message)
        raise BranchPushError(message)

    @staticmethod
    def render_changelog_markdown(
        version: semver.Version,
        changelog: dict[Optional[str], dict[CommitType, list[str]]],
        rendered_at: Optional[datetime.datetime] = None,
    ) -> str:
        """
        Render changelog markdown in the same format used for changelog files
        :param version: The version the changelog is for
        :param changelog: The changelog structure to render
        :param rendered_at: The timestamp to place in the header
        :return: Rendered markdown
        """
        if rendered_at is None:
            rendered_at = datetime.datetime.now()

        lines = [f"## {version} ({rendered_at.strftime('%Y-%m-%d %H:%M')})"]
        for scope, commits in sorted(changelog.items(), key=lambda x: (x[0] is None, x[0])):
            lines.append("")
            lines.append(f"### {scope or 'Other'}")
            for commit_type, messages in sorted(commits.items(), key=lambda x: x[0]):
                if not messages:
                    continue
                lines.append("")
                lines.append(f"#### {commit_type.name}")
                for message in messages:
                    lines.append(f"- {message}")

        return "\n".join(lines) + "\n"

    def generate_changelog(
        self,
        start_commit: Optional[git.Commit],
        end_commit: git.Commit,
        changelog_message: Optional[str],
    ) -> dict[Optional[str], dict[CommitType, list[str]]]:
        """
        Generate a changelog between two commits
        :param start_commit: The first commit to check from, or None to include
        all commits up to end_commit
        :param end_commit: The last commit to check to
        :param changelog_message: An optional message to add to the changelog
        :return: A dictionary containing the changelog, with CommitType as the key
        and a list of commit messages as the value
        """
        log.info(f"Generating changelog between {start_commit} and {end_commit}")

        result: dict[Optional[str], dict[CommitType, list[str]]] = {}

        if changelog_message:
            result[None] = {CommitType.OTHER: [changelog_message]}

        if start_commit is None:
            commits = self._repository.iter_commits(end_commit)
        else:
            commits = self._repository.iter_commits(f"{start_commit}..{end_commit}")
        for commit in commits:
            commit_message = commit.message
            if isinstance(commit_message, bytes):
                commit_message = commit_message.decode("utf-8")
            changelog_messages = []
            version_update = None
            scopes: list[Optional[str]] = []
            commit_type = CommitType.OTHER
            for line in commit_message.splitlines():
                if not line:
                    continue
                changelog_match = self._changelog_regex.match(line)

                if changelog_match:
                    changelog_messages.append(changelog_match.group("message"))
                else:
                    for version_update_regex in self._version_update_regexes:
                        version_update_match = version_update_regex.regex.match(line)
                        if version_update_match:
                            if version_update:
                                version_update = max(version_update, version_update_regex.version_update)
                            else:
                                version_update = version_update_regex.version_update
                            commit_type = max(commit_type, version_update_regex.commit_type)
                            scopes_str = version_update_match.group("scopes") if "scopes" in version_update_match.groupdict() else None
                            if scopes_str:
                                scopes = list(itertools.chain(*[[self.split_scope_words(s.strip()) for s in s.split("/")] for s in scopes_str.split(",")]))
                            else:
                                scopes = [None]

            if changelog_messages:
                if version_update == VersionUpdateEnum.MAJOR:
                    changelog_messages = [
                        message + " (BREAKING CHANGE)" for message in changelog_messages
                    ]
                for scope in scopes:
                    if scope not in result:
                        result[scope] = {}
                    if commit_type not in result[scope]:
                        result[scope][commit_type] = []
                    result[scope][commit_type].extend(changelog_messages)

        return result

    WORD_BOUNDARY_RE = re.compile(
        r"""
        # Split before capitals in camelCase/PascalCase
        (?<=[a-z0-9])(?=[A-Z]) |
        # Split between acronym and word: HTTPServer -> HTTP Server
        (?<=[A-Z])(?=[A-Z][a-z]) |
        # Existing separators: snake_case, kebab-case
        [_\-]+
        """,
        re.VERBOSE,
    )

    @classmethod
    def split_scope_words(cls, scope: str) -> str:
        if not scope:
            return ""

        # First split the original into logical words
        original_words = [
            w for w in cls.WORD_BOUNDARY_RE.sub(" ", scope).split() if w
        ]

        # Title-case the spaced version
        spaced = cls.WORD_BOUNDARY_RE.sub(" ", scope)
        spaced = re.sub(r"\s+", " ", spaced).strip()
        titled = spaced.title()

        # Split the titled string into words
        titled_words = titled.split()

        # Restore fully-upper tokens from the original
        fixed_words = []
        for orig, tit in zip(original_words, titled_words):
            if orig.isupper():
                fixed_words.append(orig)
            else:
                fixed_words.append(tit)

        # If for some reason lengths differ, just append any extras from titled
        if len(titled_words) > len(fixed_words):
            fixed_words.extend(titled_words[len(fixed_words):])

        return " ".join(fixed_words)

    def add_main_tags(
        self,
        changelog_file: Optional[str] = None,
        changelog_message: Optional[str] = None,
        dry_run: bool = False,
        push: bool = False,
    ) -> bool:
        """
        Add a new version tag to the main branch of this repository
        :param changelog_file: The file to write the changelog to
        :param changelog_message: An optional message to add to the changelog
        :param dry_run: If True, only calculate and output the version without making changes
        :param push: If True, push the new tags to the remote as they are created
        :return: Whether the process was successful
        """
        preview = self.get_main_preview(
            changelog_message=changelog_message,
        )
        if preview is None:
            return False

        self._output_result(
            "previous-version",
            self._get_version_strings(preview.previous_version)[0],
        )
        self._output_result(
            "new-version",
            self._get_version_strings(preview.new_version)[0],
        )

        if dry_run:
            log.info(f"Dry run: would create version {preview.new_version}")
            return True

        if self._main_head_commit is None:
            log.error("No main branch head commit found")
            return False

        if changelog_file:
            self._main_head_commit = self.write_changelog(
                self._main_branch,
                changelog_file,
                preview.rendered_changelog_markdown,
            )

        return self._add_version_tags_to_commit(
            self._main_head_commit, preview.new_version, push
        )

    def add_dev_tags(
        self,
        dev_branch: str,
        dev_suffix: str,
        dev_version_style: DevVersionStyle,
        changelog_file: Optional[str] = None,
        changelog_message: Optional[str] = None,
        dry_run: bool = False,
        push: bool = False,
    ) -> bool:
        """
        Add a new version tag to the dev branch of this repository
        :param dev_branch: The dev branch name
        :param dev_suffix: The suffix to use for dev tags
        :param dev_version_style: The style to use for dev versions
        :param changelog_file: The file to write the changelog to
        :param changelog_message: An optional message to add to the changelog
        :param dry_run: If True, only calculate and output the version without making changes
        :param push: If True, push the new tags to the remote as they are created
        :return: Whether the process was successful
        """
        preview = self.get_dev_preview(
            dev_branch=dev_branch,
            dev_suffix=dev_suffix,
            dev_version_style=dev_version_style,
            changelog_message=changelog_message,
        )
        if preview is None:
            return False

        dev_head_commit = preview.end_commit

        self._output_result(
            "previous-version",
            self._get_version_strings(preview.previous_version)[0],
        )
        self._output_result(
            "new-version",
            self._get_version_strings(preview.new_version)[0],
        )

        if dry_run:
            log.info(f"Dry run: would create version {preview.new_version}")
            return True

        if changelog_file:
            dev_head_commit = self.write_changelog(
                dev_branch,
                changelog_file,
                preview.rendered_changelog_markdown,
            )

        log.info(f"Adding tags for {preview.new_version} on {dev_head_commit}")
        return self._add_version_tags_to_commit(
            dev_head_commit, preview.new_version, push
        )

    def get_main_preview(
        self,
        changelog_message: Optional[str] = None,
        target_ref: Optional[str] = None,
    ) -> Optional[VersionPreview]:
        target_commit = self._resolve_target_commit(target_ref, self._main_head_commit)
        if not target_commit:
            return None

        (latest_version, latest_version_commit) = self._get_latest_version(
            target_commit, False
        )

        if latest_version is None:
            log.warning("No previous version found, assuming v0.0.0")
            latest_version = semver.Version.parse("0.0.0")
            latest_version_commit = None

        if latest_version_commit == target_commit:
            log.error(
                "Cannot add new version tag to commit that already has a version tag"
            )
            return None

        version_update_type = self._get_version_update_type(
            latest_version_commit,
            target_commit,
        )
        new_version = self._bump_version(latest_version, version_update_type)
        changelog = self.generate_changelog(
            latest_version_commit,
            target_commit,
            changelog_message,
        )
        return VersionPreview(
            previous_version=latest_version,
            new_version=new_version,
            start_commit=latest_version_commit,
            end_commit=target_commit,
            changelog=changelog,
            rendered_changelog_markdown=self.render_changelog_markdown(
                new_version,
                changelog,
            ),
        )

    def get_dev_preview(
        self,
        dev_branch: str,
        dev_suffix: str,
        dev_version_style: DevVersionStyle,
        changelog_message: Optional[str] = None,
        target_ref: Optional[str] = None,
    ) -> Optional[VersionPreview]:
        dev_head_commit = self._resolve_target_commit(
            target_ref,
            self._get_branch_head_commit(dev_branch),
        )
        if not dev_head_commit:
            return None

        if self._main_head_commit is None:
            log.error("No main branch head commit found")
            return None

        (latest_main_version, _) = self._get_latest_version(
            self._main_head_commit, False
        )
        (latest_dev_version, latest_dev_version_commit) = self._get_latest_version(
            dev_head_commit
        )

        if latest_main_version is None:
            log.warning("No previous main version found, assuming v0.0.0")
            latest_main_version = semver.Version.parse("0.0.0")

        if latest_dev_version is None:
            log.warning("No previous dev version found, assuming v0.0.0")
            latest_dev_version = semver.Version.parse("0.0.0")
            latest_dev_version_commit = None

        if latest_dev_version_commit == dev_head_commit:
            log.error(
                "Cannot add new version tag to commit that already has a version tag"
            )
            return None

        common_ancestors = self._repository.merge_base(
            self._main_head_commit,
            dev_head_commit,
        )
        if len(common_ancestors) != 1:
            log.error(
                f"Could not find a single common ancestor between {dev_head_commit} and {self._main_head_commit}"
            )
            return None

        version_update_type = self._get_version_update_type(
            common_ancestors[0],
            dev_head_commit,
        )

        dev_version_update_type = self._get_version_update_type(
            latest_dev_version_commit,
            dev_head_commit,
        )

        log.debug(f"Latest main version: {latest_main_version}")
        log.debug(f"Latest dev version: {latest_dev_version}")
        log.debug(f"Version update type: {version_update_type}")
        log.debug(f"Dev version update type: {dev_version_update_type}")

        new_dev_version = self._bump_version(latest_main_version, version_update_type)
        latest_dev_version_prerelease_bits = []
        if latest_dev_version.prerelease:
            latest_dev_version_prerelease_bits = latest_dev_version.prerelease.split(
                "."
            )[1:]
            log.debug(
                f"Latest dev version prerelease bits: {latest_dev_version_prerelease_bits}"
            )
            if (
                dev_version_style == DevVersionStyle.INCREMENTING
                and len(latest_dev_version_prerelease_bits) > 1
            ):
                log.debug("Updating prerelease to incrementing")
                latest_dev_version = latest_dev_version.replace(
                    prerelease=f"{dev_suffix}.{latest_dev_version_prerelease_bits[0]}"
                )
            elif (
                dev_version_style == DevVersionStyle.SEMANTIC
                and len(latest_dev_version_prerelease_bits) == 1
            ):
                log.debug("Updating prerelease to semantic")
                latest_dev_version = latest_dev_version.replace(
                    prerelease=f"{dev_suffix}.{latest_dev_version_prerelease_bits[0]}.0.0"
                )
        else:
            if dev_version_style == DevVersionStyle.INCREMENTING:
                latest_dev_version = latest_dev_version.replace(
                    prerelease=f"{dev_suffix}.0"
                )
                new_dev_version = new_dev_version.replace(prerelease=f"{dev_suffix}.0")
            else:
                latest_dev_version = latest_dev_version.replace(
                    prerelease=f"{dev_suffix}.0.0.0"
                )
                new_dev_version = new_dev_version.replace(
                    prerelease=f"{dev_suffix}.0.0.0"
                )

        log.debug(f"New dev version: {new_dev_version}")
        log.debug(f"Latest dev version: {latest_dev_version}")

        if (
            dev_version_style == DevVersionStyle.INCREMENTING
            or dev_version_update_type == VersionUpdateEnum.PATCH
        ):
            if not new_dev_version.prerelease and dev_version_style == DevVersionStyle.SEMANTIC:
                new_dev_version = new_dev_version.replace(
                    prerelease=f"{dev_suffix}.0.0.0"
                )

            log.debug("Incrementing dev version, or patch update")
            if (
                new_dev_version.major,
                new_dev_version.minor,
                new_dev_version.patch,
            ) == (
                latest_dev_version.major,
                latest_dev_version.minor,
                latest_dev_version.patch,
            ):
                new_dev_version = latest_dev_version.bump_prerelease(dev_suffix)
            else:
                new_dev_version = new_dev_version.bump_prerelease(dev_suffix)
            log.debug(f"New dev version: {new_dev_version}")
        else:
            log.debug("Semantic dev versioning")
            if (
                new_dev_version.major,
                new_dev_version.minor,
                new_dev_version.patch,
            ) == (
                latest_dev_version.major,
                latest_dev_version.minor,
                latest_dev_version.patch,
            ):
                try:
                    prerelease_version = semver.Version.parse(
                        ".".join(latest_dev_version_prerelease_bits)
                    )
                except ValueError:
                    prerelease_version = semver.Version.parse(
                        latest_dev_version_prerelease_bits[0]
                    )

                log.debug(f"Old prerelease version: {prerelease_version}")
                prerelease_version = self._bump_version(
                    prerelease_version, dev_version_update_type
                )
                log.debug(f"New prerelease version: {prerelease_version}")
                new_dev_version = new_dev_version.replace(
                    prerelease=f"{dev_suffix}.{prerelease_version}"
                )
            else:
                new_dev_version = new_dev_version.replace(
                    prerelease=f"{dev_suffix}.0.0.1"
                )

            log.debug(f"New dev version: {new_dev_version}")

        changelog = self.generate_changelog(
            latest_dev_version_commit,
            dev_head_commit,
            changelog_message,
        )
        return VersionPreview(
            previous_version=latest_dev_version,
            new_version=new_dev_version,
            start_commit=latest_dev_version_commit,
            end_commit=dev_head_commit,
            changelog=changelog,
            rendered_changelog_markdown=self.render_changelog_markdown(
                new_dev_version,
                changelog,
            ),
        )

    def _add_version_tags_to_commit(
        self,
        commit: git.Commit,
        version: semver.Version,
        push: bool = False,
    ) -> bool:
        """
        Add a version tag to a specific commit
        :param commit: The commit to add the tag to
        :param version: The version to use for the tag name
        :param push: If True, push each tag to origin as it is created, refusing
        to move an existing remote release tag unless allow_tag_move is set
        :return: Whether this process was successful
        """
        self._ensure_not_preview("create version tags")

        tag_names = self._get_version_strings(version)

        for index, tag_name in enumerate(tag_names):
            # _get_version_strings returns the unique, immutable full version
            # tag first, followed by any rolling alias tags produced by
            # include_shorter_versions (e.g. v1, v1.2, v1-dev). Alias tags are
            # designed to move to each new release; the full release tag is not.
            is_alias = index > 0
            self._create_version_tag(tag_name, commit, push, is_alias)

        return True

    def _create_version_tag(
        self,
        tag_name: str,
        commit: git.Commit,
        push: bool,
        is_alias: bool,
    ) -> None:
        """
        Create a single version tag and, when pushing, publish it safely.

        When pushing, the tag is published with an explicit, non-destructive
        ``refs/tags/<tag>`` refspec (never ``--tags`` and never ``--force``). If
        the tag already exists on the remote pointing at a different commit:

        - for the unique full **release** tag, the run fails (unless
          allow_tag_move is set) so a concurrent or duplicate run can never
          delete or repoint a published release tag;
        - for a rolling **alias** tag (from include_shorter_versions), the tag is
          force-moved to the new commit, since advancing those aliases every
          release is exactly what that option is for.

        Either way a tag is only ever created once its commit has landed on the
        remote (the branch push is confirmed first).
        :param tag_name: The tag to create
        :param commit: The commit the tag should point at
        :param push: Whether to publish the tag to origin
        :param is_alias: Whether this is a rolling alias tag (vs the release tag)
        """
        if not push:
            # Local-only tagging: no remote interaction at all.
            self._ensure_local_tag(tag_name, commit)
            return

        remote_sha = self._remote_tag_commit(tag_name)
        if remote_sha is not None:
            if remote_sha == commit.hexsha:
                log.info(
                    f"Tag '{tag_name}' already points at {commit} on origin; "
                    f"nothing to do"
                )
                self._ensure_local_tag(tag_name, commit)
                return

            if not is_alias and not self._allow_tag_move:
                raise TagConflictError(
                    f"Refusing to move remote release tag '{tag_name}': it already "
                    f"exists on origin at {remote_sha} but this run wants it on "
                    f"{commit.hexsha}. Set allow-tag-move to true to override."
                )

            reason = "rolling alias tag" if is_alias else "allow-tag-move enabled"
            log.warning(
                f"Moving remote tag '{tag_name}' from {remote_sha} to "
                f"{commit.hexsha} ({reason})"
            )
            self._ensure_local_tag(tag_name, commit)
            self._push_ref(f"+refs/tags/{tag_name}", f"tag '{tag_name}'")
            return

        # No remote tag yet: create it locally and publish non-destructively.
        self._ensure_local_tag(tag_name, commit)
        log.info(f"Pushing tag '{tag_name}' to origin at {commit}")
        self._push_ref(f"refs/tags/{tag_name}", f"tag '{tag_name}'")

    def _ensure_local_tag(self, tag_name: str, commit: git.Commit) -> None:
        """
        Ensure a local tag with the given name points at the given commit,
        recreating it if it currently points elsewhere.
        :param tag_name: The tag to create or move locally
        :param commit: The commit the tag should point at
        """
        existing = next(
            (tag for tag in self._repository.tags if tag.name == tag_name), None
        )
        if existing is not None:
            if existing.commit.hexsha == commit.hexsha:
                return
            self._repository.delete_tag(existing)

        log.info(f"Adding tag '{tag_name}' to commit '{commit}'")
        self._repository.create_tag(tag_name, ref=str(commit))

    def _remote_tag_commit(self, tag_name: str) -> Optional[str]:
        """
        Return the commit sha the remote tag points at, or None if the tag does
        not exist on the remote.
        :param tag_name: The tag to look up on origin
        :return: The commit sha (peeled for annotated tags), or None
        """
        output = self._repository.git.ls_remote("origin", f"refs/tags/{tag_name}")
        if not output.strip():
            return None

        direct: Optional[str] = None
        peeled: Optional[str] = None
        for line in output.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.endswith("^{}"):
                peeled = sha
            else:
                direct = sha

        # Prefer the peeled sha (the underlying commit of an annotated tag);
        # for the lightweight tags this action creates, only ``direct`` exists.
        return peeled or direct

    def is_stale_trigger(self, branch_name: str) -> bool:
        """
        Detect whether this run is operating on a superseded commit.

        The checked-out HEAD (typically ``github.sha``) is compared against the
        live remote tip of the target branch. Duplicate webhook deliveries can
        launch two runs at the same triggering sha; if another run has already
        advanced the branch, this one is stale and should not mutate anything.
        :param branch_name: The target branch to compare against
        :return: True if HEAD differs from the live remote tip of the branch
        """
        try:
            local_head = self._repository.head.commit.hexsha
        except (ValueError, git.exc.GitError):
            return False

        try:
            output = self._repository.git.ls_remote(
                "origin", f"refs/heads/{branch_name}"
            )
        except git.exc.GitCommandError as exc:
            log.warning(f"Could not check remote tip of '{branch_name}': {exc}")
            return False

        if not output.strip():
            # The branch does not exist on the remote yet; nothing to be stale
            # against.
            return False

        remote_tip = output.split()[0]
        if local_head != remote_tip:
            log.warning(
                f"Checked-out HEAD {local_head} does not match the live tip of "
                f"'{branch_name}' on origin ({remote_tip}); this run is operating "
                f"on a superseded commit."
            )
            return True

        return False

    def _get_branch_head_commit(self, branch_name: str) -> Optional[git.Commit]:
        """
        Get the head commit from the specified branch
        :param branch_name: The branch name to get the commit for
        :return: The commit object, if the branch exists, otherwise None
        """
        log.info(f"Searching for branch: {branch_name}")
        for branch in self._repository.branches:
            log.debug(f"Checking branch {branch.name}")
            if branch.name == branch_name:
                return branch.commit

        remote = self._repository.remote()
        remote_name = remote.name
        remote_branches = remote.refs
        for branch in remote_branches:
            log.debug(f"Checking branch {branch.name}")
            name_bits = branch.name.split("/")
            if len(name_bits) != 2:
                continue
            if name_bits[0] == remote_name and name_bits[1] == branch_name:
                return branch.commit

        return None

    def _get_version_update_type(
        self,
        start_commit: Optional[git.Commit],
        end_commit: git.Commit,
    ) -> VersionUpdateEnum:
        """
        Iterate over all commits between start_commit and end_commit to determine
        what kind of version update should be applied
        :param start_commit: The first commit to check from, or None to check all
        commits up to end_commit
        :param end_commit: The last commit to check to
        :return: The VersionUpdateEnum value specifying the type of version update
        """
        version_update = VersionUpdateEnum.PATCH
        for commit in self._iter_commits_between(start_commit, end_commit):
            commit_message = commit.message
            for line in commit_message.splitlines():
                for version_update_regex in self._version_update_regexes:
                    if (
                        version_update_regex.regex.match(line)
                        and version_update_regex.version_update > version_update
                    ):
                        version_update = version_update_regex.version_update

        return version_update

    def _get_latest_version(
        self,
        commit: git.Commit,
        include_prerelease: bool = True,
    ) -> Tuple[Optional[semver.Version], Optional[git.Commit]]:
        """
        Get the latest version going back in time from the specified commit
        :param commit: The commit to work backwards from
        :param include_prerelease: Whether to include versions with prerelease
        values or not
        :return: A Tuple containing the version and commit, or (None, None)
        """

        log.info(f"Finding latest tag on {commit}")

        tags: list[Tuple[semver.Version, git.TagReference]] = []
        for tag in self._repository.tags:
            try:
                version = semver.Version.parse(tag.name[len(self._version_prefix) :])
            except ValueError:
                continue

            if version.prerelease and not include_prerelease:
                continue

            tags.append((version, tag))

        for tag_version, tag_ref in sorted(tags, key=lambda t: t[0], reverse=True):
            log.debug(f"Checking tag {tag_ref.name} on {tag_ref.commit}")
            common_ancestors = self._repository.merge_base(
                tag_ref.commit,
                commit,
            )

            if len(common_ancestors) == 1 and common_ancestors[0] == tag_ref.commit:
                log.info(f"Returning version: {tag_version}")
                return tag_version, tag_ref.commit

        log.warning(f"Not found latest version on {commit}")
        return None, None

    def _resolve_target_commit(
        self,
        target_ref: Optional[str],
        default_commit: Optional[git.Commit],
    ) -> Optional[git.Commit]:
        if target_ref is None:
            return default_commit

        log.info(f"Resolving target ref: {target_ref}")
        try:
            return self._repository.commit(target_ref)
        except (git.BadName, ValueError):
            log.error(f"Target ref not found: {target_ref}")
            return None

    def _iter_commits_between(
        self,
        start_commit: Optional[git.Commit],
        end_commit: git.Commit,
    ):
        if start_commit is None:
            return self._repository.iter_commits(end_commit)
        return self._repository.iter_commits(f"{start_commit}..{end_commit}")

    def _get_version_strings(self, version: semver.Version) -> list[str]:
        suffix = ""
        if version.prerelease:
            suffix = "-" + version.prerelease.split(".")[0]

        result = [f"{self._version_prefix}{version}"]

        if not self._include_shorter_versions:
            return result

        if suffix:
            result.append(
                f"{self._version_prefix}{version.major}.{version.minor}.{version.patch}{suffix}"
            )

        result.extend(
            [
                f"{self._version_prefix}{version.major}.{version.minor}{suffix}",
                f"{self._version_prefix}{version.major}{suffix}",
            ]
        )

        return result

    @staticmethod
    def _bump_version(
        previous_version: semver.Version,
        update_type: VersionUpdateEnum,
    ) -> semver.Version:
        """
        Create a new semver.Version object with the appropriate element bumped
        :param previous_version: The previous version to bump up
        :param update_type: Which element of the previous version to bump
        :return: The new version
        """
        if update_type == VersionUpdateEnum.PATCH:
            return previous_version.bump_patch()
        elif update_type == VersionUpdateEnum.MINOR:
            return previous_version.bump_minor()
        elif update_type == VersionUpdateEnum.MAJOR:
            return previous_version.bump_major()
        else:
            return previous_version

    @staticmethod
    def _output_result(name: str, value: str) -> None:
        log.info(f"Writing output {name}: '{value}'")
        github_output = os.getenv("GITHUB_OUTPUT")
        if not github_output:
            log.error("GITHUB_OUTPUT not set")
            return

        with open(github_output, "a") as fd:
            if "\n" in value:
                delimiter = f"EOF_{uuid.uuid4().hex}"
                fd.write(f"{name}<<{delimiter}\n{value}{delimiter}\n")
            else:
                fd.write(f"{name}={value}\n")


def parse_args(args: list[str]) -> Optional[argparse.Namespace]:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-r",
        "--repository",
        default=os.getenv("GITHUB_WORKSPACE", os.getcwd()),
        help="Path to the repository to work on",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        default=False,
        help="Don't fetch the repository",
    )
    parser.add_argument(
        "-m",
        "--main-branch",
        default=os.getenv("MAIN_BRANCH", "main"),
        help="The name of the main branch",
    )
    parser.add_argument(
        "-d",
        "--dev-branch",
        default=os.getenv("DEV_BRANCH"),
        help="The name of the dev branch (if applying a dev version tag)",
    )
    parser.add_argument(
        "-s",
        "--dev-suffix",
        default=os.getenv("DEV_SUFFIX", "dev"),
        help="The suffix to use for the dev branch",
    )

    parser.add_argument(
        "-i",
        "--include-shorter-versions",
        default=(
            os.getenv("INCLUDE_SHORTER_VERSIONS", "0").lower()
            in ["1", "on", "yes", "y", "true", "t"]
        ),
        action="store_true",
        help="Include shorter versions of tags that move as new versions are created",
    )
    parser.add_argument(
        "-p",
        "--push",
        action="store_true",
        default=(
            os.getenv("PUSH", "0").lower() in ["1", "on", "yes", "y", "true", "t"]
        ),
        help="Push any new tags to the remote repository",
    )

    parser.add_argument(
        "-v",
        "--use_semantic_dev_versions",
        action="store_true",
        default=(
            os.getenv("USE_SEMANTIC_DEV_VERSIONS", "0").lower()
            in ["1", "on", "yes", "y", "true", "t"]
        ),
        help="Use semantic dev versions",
    )

    parser.add_argument(
        "-c",
        "--changelog-file",
        default=os.getenv("CHANGELOG_FILE"),
        help="The file to write changelog to",
    )

    parser.add_argument(
        "-g",
        "--changelog-message",
        default=os.getenv("CHANGELOG_MESSAGE"),
        help="An optional changelog message to add",
    )

    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=(
            os.getenv("DRY_RUN", "0").lower() in ["1", "on", "yes", "y", "true", "t"]
        ),
        help="Only calculate and output the version without creating tags or writing changelog",
    )
    parser.add_argument(
        "--allow-tag-move",
        action="store_true",
        default=(
            os.getenv("ALLOW_TAG_MOVE", "0").lower()
            in ["1", "on", "yes", "y", "true", "t"]
        ),
        help=(
            "Allow force-moving an existing remote tag that points at a different "
            "commit. Off by default so a concurrent or duplicate run can never "
            "repoint a published tag."
        ),
    )
    parser.add_argument(
        "--stale-trigger",
        choices=["ignore", "skip", "fail"],
        default=os.getenv("STALE_TRIGGER", "ignore"),
        help=(
            "How to handle a run whose checked-out HEAD no longer matches the live "
            "remote tip of the target branch (a superseded/duplicate trigger). "
            "'ignore' (default) proceeds, 'skip' exits 0 without mutating, 'fail' "
            "exits non-zero."
        ),
    )
    parser.add_argument(
        "--preview-changelog",
        action="store_true",
        default=(
            os.getenv("PREVIEW_CHANGELOG", "0").lower()
            in ["1", "on", "yes", "y", "true", "t"]
        ),
        help="Preview the current changelog section and version without mutating git state",
    )
    parser.add_argument(
        "--target-ref",
        default=os.getenv("TARGET_REF"),
        help="Target ref or commit to preview instead of the current branch head",
    )
    parser.add_argument(
        "--print-changelog",
        action="store_true",
        default=(
            os.getenv("PRINT_CHANGELOG", "0").lower()
            in ["1", "on", "yes", "y", "true", "t"]
        ),
        help="Print the rendered changelog markdown to stdout",
    )
    parser.add_argument(
        "--preview-output-file",
        default=os.getenv("PREVIEW_OUTPUT_FILE"),
        help="Write the rendered preview changelog markdown to this file",
    )

    result = parser.parse_args(args)

    if result.dev_branch and not result.dev_suffix:
        parser.print_help()
        return None

    if result.target_ref and not result.preview_changelog:
        parser.error("--target-ref requires --preview-changelog")

    return result


def _write_preview_outputs(
    versioner: SemanticVersioner,
    preview: VersionPreview,
    print_changelog: bool,
    preview_output_file: Optional[str],
) -> None:
    versioner._output_result(
        "previous-version",
        versioner._get_version_strings(preview.previous_version)[0],
    )
    versioner._output_result(
        "new-version",
        versioner._get_version_strings(preview.new_version)[0],
    )
    versioner._output_result(
        "rendered-changelog",
        preview.rendered_changelog_markdown,
    )

    if print_changelog:
        sys.stdout.write(preview.rendered_changelog_markdown)

    if preview_output_file:
        with open(preview_output_file, "w") as fd:
            fd.write(preview.rendered_changelog_markdown)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args:
        return 1

    log.info(f"Repository: {args.repository}")
    log.info(f"Main branch: {args.main_branch}")
    log.info(f"Changelog file: {args.changelog_file}")
    log.info(f"Dry run: {args.dry_run}")
    log.info(f"Preview changelog: {args.preview_changelog}")

    versioner = SemanticVersioner(
        args.repository,
        args.no_fetch,
        args.main_branch,
        args.include_shorter_versions,
        preview=args.preview_changelog,
        allow_tag_move=args.allow_tag_move,
    )
    if not versioner.initialize():
        return 1

    if args.preview_changelog:
        if args.dev_branch:
            log.info(f"Dev branch: {args.dev_branch}")
            log.info(f"Dev suffix: {args.dev_suffix}")
            log.info(f"Using semantic dev versions: {args.use_semantic_dev_versions}")
            preview = versioner.get_dev_preview(
                args.dev_branch,
                args.dev_suffix,
                (
                    DevVersionStyle.SEMANTIC
                    if args.use_semantic_dev_versions
                    else DevVersionStyle.INCREMENTING
                ),
                args.changelog_message,
                args.target_ref,
            )
        else:
            preview = versioner.get_main_preview(
                args.changelog_message,
                args.target_ref,
            )

        if preview is None:
            return 1

        _write_preview_outputs(
            versioner,
            preview,
            args.print_changelog,
            args.preview_output_file,
        )
        return 0

    # Stale-trigger safeguard: bail out before mutating anything if this run is
    # operating on a commit that a concurrent/duplicate run has already
    # superseded on the target branch.
    if not args.dry_run and args.stale_trigger != "ignore":
        target_branch = args.dev_branch or args.main_branch
        if versioner.is_stale_trigger(target_branch):
            if args.stale_trigger == "fail":
                log.error(
                    "Refusing to run against a superseded commit "
                    "(stale-trigger=fail)"
                )
                return 1
            log.warning(
                "Skipping run against a superseded commit (stale-trigger=skip)"
            )
            return 0

    push = args.push and not args.dry_run

    try:
        if args.dev_branch:
            log.info(f"Dev branch: {args.dev_branch}")
            log.info(f"Dev suffix: {args.dev_suffix}")
            log.info(f"Using semantic dev versions: {args.use_semantic_dev_versions}")

            if not versioner.add_dev_tags(
                args.dev_branch,
                args.dev_suffix,
                (
                    DevVersionStyle.SEMANTIC
                    if args.use_semantic_dev_versions
                    else DevVersionStyle.INCREMENTING
                ),
                args.changelog_file,
                args.changelog_message,
                args.dry_run,
                push,
            ):
                return 1
        else:
            if not versioner.add_main_tags(
                args.changelog_file,
                args.changelog_message,
                args.dry_run,
                push,
            ):
                return 1
    except SemanticVersionerError as exc:
        # These are already logged at ERROR where raised; keep the exit clean.
        log.error(f"Aborting: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
