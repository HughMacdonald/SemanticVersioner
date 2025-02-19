import argparse
import os
import re
import sys
from enum import IntEnum
from typing import Optional, Tuple

import git
import semver


class VersionUpdateEnum(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


class FilamentVersioner:
    # Regular expressions to be run on commit messages to determine which
    # version part to update
    _version_update_regexes = [
        (
            # Commit message starting with (case insensitive):
            # fix(anything):
            # fix:
            re.compile("^[Ff][Ii][Xx](\(.*\))?:"),
            VersionUpdateEnum.PATCH,
        ),
        (
            # Commit message starting with (case insensitive):
            # feat(anything):
            # feat:
            re.compile("^[Ff][Ee][Aa][Tt](\(.*\))?:"),
            VersionUpdateEnum.MINOR,
        ),
        (
            # Commit message starting with (case insensitive):
            # feat(anything)!:
            # feat!:
            # fix(anything)!:
            # fix!:
            re.compile("^([Ff][Ee][Aa][Tt]|[Ff][Ii][Xx])(\(.*\))?!:"),
            VersionUpdateEnum.MAJOR,
        ),
    ]

    # The string to be used as the prefix for all versions. The tags are
    # expected to be this, followed by the string representation of the
    # semver.Version object
    _version_prefix = "v"

    def __init__(
        self,
        repository: str,
        main_branch: str,
    ):
        self._repository = git.Repo(repository)
        self._main_branch = main_branch
        self._main_head_commit: Optional[git.Commit] = None

    def initialize(self) -> bool:
        """
        Initialize the object
        :return: Whether the initialization was successful
        """
        self._main_head_commit = self._get_branch_head_commit(self._main_branch)
        if not self._main_head_commit:
            print(f"Branch not found: {self._main_branch}")
            branch_names = [branch.name for branch in self._repository.branches]
            print(f"Available branches: {', '.join(branch_names)}")
            return False

        return True

    def add_main_tag(self) -> bool:
        """
        Add a new version tag to the main branch of this repository
        :return: Whether the process was successful
        """
        (latest_version, latest_version_commit) = self._get_latest_version(
            self._main_head_commit, False
        )
        if latest_version_commit == self._main_head_commit:
            print("Cannot add new version tag to commit that already has a version tag")
            return False

        version_update_type = self._get_version_update_type(
            latest_version_commit,
            self._main_head_commit,
        )
        self._output_result(
            "previous-version",
            self._get_version_string(latest_version),
        )

        new_version = self._bump_version(latest_version, version_update_type)

        self._output_result(
            "new-version",
            self._get_version_string(new_version),
        )

        return self._add_version_tag_to_commit(self._main_head_commit, new_version)

    def add_dev_tag(
        self,
        dev_branch: str,
        dev_suffix: str,
    ) -> bool:
        """
        Add a new version tag to the dev branch of this repository
        :param dev_branch: The dev branch name
        :param dev_suffix: The suffix to use for dev tags
        :return: Whether the process was successful
        """
        dev_head_commit = self._get_branch_head_commit(dev_branch)
        (latest_main_version, latest_main_version_commit) = self._get_latest_version(
            self._main_head_commit
        )
        (latest_dev_version, latest_dev_version_commit) = self._get_latest_version(
            dev_head_commit
        )

        if latest_dev_version_commit == dev_head_commit:
            print("Cannot add new version tag to commit that already has a version tag")
            return False

        common_ancestors = self._repository.merge_base(
            self._main_head_commit,
            dev_head_commit,
        )
        if len(common_ancestors) != 1:
            print("Could not find a single common ancestor")
            return False

        version_update_type = self._get_version_update_type(
            common_ancestors[0],
            dev_head_commit,
        )

        self._output_result(
            "previous-version",
            self._get_version_string(latest_dev_version),
        )

        new_dev_version = self._bump_version(latest_main_version, version_update_type)

        if (new_dev_version.major, new_dev_version.minor, new_dev_version.patch) == (
            latest_dev_version.major,
            latest_dev_version.minor,
            latest_dev_version.patch,
        ):
            new_dev_version = latest_dev_version.bump_prerelease(dev_suffix)
        else:
            new_dev_version = new_dev_version.bump_prerelease(dev_suffix)

        self._output_result(
            "new-version",
            self._get_version_string(new_dev_version),
        )

        return self._add_version_tag_to_commit(dev_head_commit, new_dev_version)

    def push_tags(self) -> bool:
        """
        Push all tags to the remote repository
        :return: Whether the process was successful
        """
        self._repository.git.push("origin", "--tags")
        return True

    def _add_version_tag_to_commit(
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
        tag_name = self._get_version_string(version)

        if tag_name in [tag.name for tag in self._repository.tags]:
            print(f"Tag '{tag_name}' already exists")
            return False

        print(f"Adding tag '{tag_name}' to commit '{commit}'")
        self._repository.create_tag(tag_name, ref=str(commit))
        return True

    def _get_branch_head_commit(self, branch_name: str) -> Optional[git.Commit]:
        """
        Get the head commit from the specified branch
        :param branch_name: The branch name to get the commit for
        :return: The commit object, if the branch exists, otherwise None
        """
        print(f"Searching for branch: {branch_name}")
        for branch in self._repository.branches:
            print(f"Checking branch {branch.name}")
            if branch.name == branch_name:
                return branch.commit

        remote = self._repository.remote()
        remote.fetch(tags=True)
        remote_name = remote.name
        remote_branches = remote.refs
        for branch in remote_branches:
            print(f"Checking branch {branch.name}")
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
        version_update = VersionUpdateEnum.NONE
        for commit in self._repository.iter_commits(f"{start_commit}..{end_commit}"):
            commit_message = commit.message
            for version_update_regex in self._version_update_regexes:
                if (
                    version_update_regex[0].match(commit_message)
                    and version_update_regex[1] > version_update
                ):
                    version_update = version_update_regex[1]

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
        tag_name = self._repository.git.describe(commit, tags=True, abbrev=0)
        if tag_name.startswith(self._version_prefix):
            try:
                tag_commit = None
                for tag in self._repository.tags:
                    if tag.name == tag_name:
                        tag_commit = tag.commit
                        break
                version = semver.Version.parse(tag_name[len(self._version_prefix) :])
                if version.prerelease and not include_prerelease:
                    if commit.parents:
                        return self._get_latest_version(commit.parents[0])
                    else:
                        return None, None
                return version, tag_commit
            except ValueError:
                if commit.parents:
                    return self._get_latest_version(commit.parents[0])
        else:
            if commit.parents:
                return self._get_latest_version(commit.parents[0])

        return None, None

    def _get_version_string(self, version: semver.Version) -> str:
        return self._version_prefix + str(version)

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
        github_output = os.getenv("GITHUB_OUTPUT")
        if not github_output:
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
        "-p",
        "--push",
        action="store_true",
        default=os.getenv("PUSH"),
        help="Push any new tags to the remote repository",
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

    print(f"Repository: {args.repository}")
    print(f"Main branch: {args.main_branch}")

    versioner = FilamentVersioner(args.repository, args.main_branch)
    if not versioner.initialize():
        return 1

    if args.dev_branch:
        print(f"Dev branch: {args.dev_branch}")
        print(f"Dev suffix: {args.dev_suffix}")
        if not versioner.add_dev_tag(args.dev_branch, args.dev_suffix):
            return 1
    else:
        if not versioner.add_main_tag():
            return 1

    if args.push:
        if not versioner.push_tags():
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
