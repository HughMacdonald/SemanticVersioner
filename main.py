import argparse
import datetime
import logging
import os
import re
import sys
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


class CommitType(IntEnum):
    FEATURE = 0
    FIX = 1
    OTHER = 2


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


class SemanticVersioner:
    _changelog_regex = re.compile(r"^CHANGELOG:\s*(?P<message>.*)$")

    # Regular expressions to be run on commit messages to determine which
    # version part to update
    _version_update_regexes: list[VersionUpdateRegex] = [
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # fix(anything):
            # fix:
            regex=re.compile(r"^fix(\((?P<scope>.*)\))?:", re.I),
            version_update=VersionUpdateEnum.PATCH,
            commit_type=CommitType.FIX,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything):
            # feat:
            regex=re.compile(r"^feat(\((?P<scope>.*)\))?:", re.I),
            version_update=VersionUpdateEnum.MINOR,
            commit_type=CommitType.FEATURE,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything)!:
            # feat!:
            # fix(anything)!:
            # fix!:
            regex=re.compile(r"^fix(\((?P<scope>.*)\))?!:", re.I),
            version_update=VersionUpdateEnum.MAJOR,
            commit_type=CommitType.FIX,
        ),
        VersionUpdateRegex(
            # Commit message starting with (case insensitive):
            # feat(anything)!:
            # feat!:
            # fix(anything)!:
            # fix!:
            regex=re.compile(r"^feat(\((?P<scope>.*)\))?!:", re.I),
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

    def __init__(
        self,
        repository_path: str,
        no_fetch: bool,
        main_branch: str,
        include_shorter_versions: bool,
    ):
        self._repository = git.Repo(repository_path)
        self._no_fetch = no_fetch
        self._main_branch = main_branch
        self._main_head_commit: Optional[git.Commit] = None
        self._include_shorter_versions = include_shorter_versions

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
        version: semver.Version,
        changelog: dict[Optional[str], dict[CommitType, list[str]]],
    ) -> git.Commit:
        """
        Write the changelog to the specified file
        :param branch_name: The name of the branch to write the changelog for
        :param changelog_file: The file to write the changelog to
        :param version: The version the changelog is for
        :param changelog: The changelog to write
        :return: The commit object for the new commit
        """
        existing_changelog = None

        log.info(f"Writing changelog file to {changelog_file}")

        if os.path.isfile(changelog_file):
            with open(changelog_file, "r") as fd:
                existing_changelog = fd.read()

        with open(changelog_file, "w") as fd:
            fd.write(f"## {version} ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})\n")
            for scope, commits in sorted(changelog.items(), key=lambda x: (x[0] is None, x[0])):
                if not scope:
                    scope = "Other"
                fd.write(f"\n### {self.split_scope_words(scope)}\n")
                for commit_type, messages in sorted(commits.items(), key=lambda x: x[0]):
                    if messages:
                        fd.write(f"\n#### {commit_type.name}\n")
                        for message in messages:
                            fd.write(f"- {message}\n")
            if existing_changelog:
                fd.write("\n")
                fd.write(existing_changelog)

        log.info("Committing changelog")
        self._repository.git.checkout(branch_name)
        self._repository.index.add([changelog_file])
        new_commit = self._repository.index.commit(f"Update changelog for {version}")
        origin = self._repository.remote(name="origin")
        origin.push(branch_name)

        return new_commit

    def generate_changelog(
        self,
        start_commit: git.Commit,
        end_commit: git.Commit,
        changelog_message: Optional[str],
    ) -> dict[Optional[str], dict[CommitType, list[str]]]:
        """
        Generate a changelog between two commits
        :param start_commit: The first commit to check from
        :param end_commit: The last commit to check to
        :param changelog_message: An optional message to add to the changelog
        :return: A dictionary containing the changelog, with CommitType as the key
        and a list of commit messages as the value
        """
        log.info(f"Generating changelog between {start_commit} and {end_commit}")

        result: dict[Optional[str], dict[CommitType, list[str]]] = {}

        if changelog_message:
            result[None] = {CommitType.OTHER: [changelog_message]}

        for commit in self._repository.iter_commits(f"{start_commit}..{end_commit}"):
            commit_message = commit.message
            changelog_messages = []
            version_update = None
            scope = None
            commit_type = CommitType.OTHER
            for line in commit_message.splitlines():
                changelog_match = self._changelog_regex.match(line)

                if changelog_match:
                    changelog_messages.append(changelog_match.group("message"))

                for version_update_regex in self._version_update_regexes:
                    version_update_match = version_update_regex.regex.match(line)
                    if version_update_match:
                        version_update = version_update_regex.version_update
                        commit_type = version_update_regex.commit_type
                        scope = version_update_match.group("scope")

            if changelog_messages:
                if version_update == VersionUpdateEnum.MAJOR:
                    changelog_messages = [
                        message + " (BREAKING CHANGE)" for message in changelog_messages
                    ]
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
    """, re.VERBOSE
        )

    @classmethod
    def split_scope_words(cls, scope: str) -> str:
        scope_bits = scope.split(",")
        if len(scope_bits) > 1:
            return ", ".join([cls.split_scope_words(scope_bit) for scope_bit in scope_bits])

        scope_bits = scope.split("/")
        if len(scope_bits) > 1:
            return "/".join([cls.split_scope_words(scope_bit) for scope_bit in scope_bits])

        if not scope:
            return ""

        spaced = cls.WORD_BOUNDARY_RE.sub(" ", scope)
        spaced = re.sub(r"\s+", " ", spaced).strip()
        return spaced.title()

    def add_main_tags(
        self,
        changelog_file: Optional[str] = None,
        changelog_message: Optional[str] = None,
    ) -> bool:
        """
        Add a new version tag to the main branch of this repository
        :param changelog_file: The file to write the changelog to
        :param changelog_message: An optional message to add to the changelog
        :return: Whether the process was successful
        """
        (latest_version, latest_version_commit) = self._get_latest_version(
            self._main_head_commit, False
        )
        if latest_version_commit == self._main_head_commit:
            log.error(
                "Cannot add new version tag to commit that already has a version tag"
            )
            return False

        version_update_type = self._get_version_update_type(
            latest_version_commit,
            self._main_head_commit,
        )
        self._output_result(
            "previous-version",
            self._get_version_strings(latest_version)[0],
        )

        new_version = self._bump_version(latest_version, version_update_type)

        self._output_result(
            "new-version",
            self._get_version_strings(new_version)[0],
        )

        if changelog_file:
            changelog = self.generate_changelog(
                latest_version_commit,
                self._main_head_commit,
                changelog_message,
            )
            self._main_head_commit = self.write_changelog(
                self._main_branch, changelog_file, new_version, changelog
            )

        return self._add_version_tags_to_commit(self._main_head_commit, new_version)

    def add_dev_tags(
        self,
        dev_branch: str,
        dev_suffix: str,
        dev_version_style: DevVersionStyle,
        changelog_file: Optional[str] = None,
        changelog_message: Optional[str] = None,
    ) -> bool:
        """
        Add a new version tag to the dev branch of this repository
        :param dev_branch: The dev branch name
        :param dev_suffix: The suffix to use for dev tags
        :param dev_version_style: The style to use for dev versions
        :param changelog_file: The file to write the changelog to
        :param changelog_message: An optional message to add to the changelog
        :return: Whether the process was successful
        """
        dev_head_commit = self._get_branch_head_commit(dev_branch)
        (latest_main_version, latest_main_version_commit) = self._get_latest_version(
            self._main_head_commit, False
        )
        (latest_dev_version, latest_dev_version_commit) = self._get_latest_version(
            dev_head_commit
        )

        if not latest_main_version:
            log.error("Could not find the latest main version")
            return False

        if not latest_dev_version:
            log.error("Could not find the latest dev version")
            return False

        if latest_dev_version_commit == dev_head_commit:
            log.error(
                "Cannot add new version tag to commit that already has a version tag"
            )
            return False

        common_ancestors = self._repository.merge_base(
            self._main_head_commit,
            dev_head_commit,
        )
        if len(common_ancestors) != 1:
            log.error(
                f"Could not find a single common ancestor between {dev_head_commit} and {self._main_head_commit}"
            )
            return False

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

        self._output_result(
            "previous-version",
            self._get_version_strings(latest_dev_version)[0],
        )

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

        self._output_result(
            "new-version",
            self._get_version_strings(new_dev_version)[0],
        )

        if changelog_file:
            changelog = self.generate_changelog(
                latest_dev_version_commit,
                dev_head_commit,
                changelog_message,
            )
            dev_head_commit = self.write_changelog(
                dev_branch,
                changelog_file,
                new_dev_version,
                changelog,
            )

        log.info(f"Adding tags for {new_dev_version} on {dev_head_commit}")
        return self._add_version_tags_to_commit(dev_head_commit, new_dev_version)

    def push_tags(self) -> bool:
        """
        Push all tags to the remote repository
        :return: Whether the process was successful
        """
        self._repository.git.push("origin", "--tags")
        return True

    def _add_version_tags_to_commit(
        self,
        commit: git.Commit,
        version: semver.Version,
    ) -> bool:
        """
        Add a version tag to a specific commit
        :param commit: The commit to add the tag to
        :param version: The version to use for the tag name
        :return: Whether this process was successful
        """
        existing_tags = {tag.name: tag for tag in self._repository.tags}
        tag_names = self._get_version_strings(version)

        for tag_name in tag_names:
            existing_tag = existing_tags.get(tag_name)
            if existing_tag:
                log.info(f"Deleting tag '{tag_name}'")
                self._repository.delete_tag(existing_tag)
                self._repository.git.push("--delete", "origin", tag_name)

            log.info(f"Adding tag '{tag_name}' to commit '{commit}'")
            self._repository.create_tag(tag_name, ref=str(commit))

        return True

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
        start_commit: git.Commit,
        end_commit: git.Commit,
    ) -> VersionUpdateEnum:
        """
        Iterate over all commits between start_commit and end_commit to determine
        what kind of version update should be applied
        :param start_commit: The first commit to check from
        :param end_commit: The last commit to check to
        :return: The VersionUpdateEnum value specifying the type of version update
        """
        version_update = VersionUpdateEnum.PATCH
        for commit in self._repository.iter_commits(f"{start_commit}..{end_commit}"):
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

        tags = []
        for tag in self._repository.tags:
            try:
                version = semver.Version.parse(tag.name[len(self._version_prefix) :])
            except ValueError:
                continue

            if version.prerelease and not include_prerelease:
                continue

            tags.append({"tag": tag, "version": version})

        for tag in sorted(tags, key=lambda t: t["version"], reverse=True):
            log.debug(f"Checking tag {tag['tag'].name} on {tag['tag'].commit}")
            common_ancestors = self._repository.merge_base(
                tag["tag"].commit,
                commit,
            )

            if len(common_ancestors) == 1 and common_ancestors[0] == tag["tag"].commit:
                log.info(f"Returning version: {tag['version']}")
                return tag["version"], tag["tag"].commit

        log.error(f"Not found latest version on {commit}")
        return None, None

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

    result = parser.parse_args(args)

    if result.dev_branch and not result.dev_suffix:
        parser.print_help()
        return None

    return result


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args:
        return 1

    log.info(f"Repository: {args.repository}")
    log.info(f"Main branch: {args.main_branch}")
    log.info(f"Changelog file: {args.changelog_file}")

    versioner = SemanticVersioner(
        args.repository, args.no_fetch, args.main_branch, args.include_shorter_versions
    )
    if not versioner.initialize():
        return 1

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
        ):
            return 1
    else:
        if not versioner.add_main_tags(
            args.changelog_file,
            args.changelog_message,
        ):
            return 1

    if args.push:
        if not versioner.push_tags():
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
