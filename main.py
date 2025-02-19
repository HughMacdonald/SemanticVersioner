import argparse
import re
import sys
from enum import IntEnum
from typing import Optional

import git
import semver


class VersionUpdateEnum(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


class FilamentVersioner:
    _version_update_regexes = [
        (re.compile("^[Ff][Ii][Xx](\(.*\))?:"), VersionUpdateEnum.PATCH),
        (re.compile("^[Ff][Ee][Aa][Tt](\(.*\))?:"), VersionUpdateEnum.MINOR),
        (
            re.compile("^([Ff][Ee][Aa][Tt]|[Ff][Ii][Xx])(\(.*\))?!:"),
            VersionUpdateEnum.MAJOR,
        ),
    ]

    _version_prefix = "v"

    def __init__(self, repository: str, main_branch: str):
        self._repository = git.Repo(repository)
        self._main_branch = main_branch
        self._main_head_commit: Optional[git.Commit] = None

    def initialize(self) -> bool:
        self._main_head_commit = self._get_branch_head_commit(self._main_branch)
        if not self._main_head_commit:
            print(f"Branch not found: {self._main_branch}")
            return False

        return True

    def add_main_tag(self) -> bool:
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
        new_dev_version = self._bump_version(latest_version, version_update_type)

        return self._add_version_tag_to_commit(self._main_head_commit, new_dev_version)

    def add_dev_tag(self, dev_branch: str, dev_suffix: str) -> bool:
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
            self._main_head_commit, dev_head_commit,
        )
        if len(common_ancestors) != 1:
            print("Could not find a single common ancestor")
            return False

        version_update_type = self._get_version_update_type(
            common_ancestors[0], dev_head_commit,
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

        return self._add_version_tag_to_commit(dev_head_commit, new_dev_version)

    def _add_version_tag_to_commit(
        self, commit: git.Commit, version: semver.Version
    ) -> bool:
        tag_name = self._version_prefix + str(version)

        if tag_name in [tag.name for tag in self._repository.tags]:
            print(f"Tag '{tag_name}' already exists")
            return False

        print(f"Adding tag '{tag_name}' to commit '{commit}'")
        self._repository.create_tag(tag_name, ref=str(commit))
        return True

    def _get_branch_head_commit(self, branch_name: str) -> Optional[git.Commit]:
        for branch in self._repository.branches:
            if branch.name == branch_name:
                return branch.commit

        return None

    def _get_version_update_type(
        self, start_commit: git.Commit, end_commit: git.Commit
    ) -> VersionUpdateEnum:
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
        self, commit: git.Commit, include_prerelease: bool = True
    ) -> Tuple[Optional[semver.Version], Optional[git.Commit]]:
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

        return None, None

    @staticmethod
    def _bump_version(
        previous_version: semver.Version, update_type: VersionUpdateEnum
    ) -> semver.Version:
        if update_type == VersionUpdateEnum.PATCH:
            return previous_version.bump_patch()
        elif update_type == VersionUpdateEnum.MINOR:
            return previous_version.bump_minor()
        elif update_type == VersionUpdateEnum.MAJOR:
            return previous_version.bump_major()
        else:
            return previous_version


def parse_args(args: list[str]) -> Optional[argparse.Namespace]:
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--repository", required=True)
    parser.add_argument("-m", "--main-branch", required=True)
    parser.add_argument("-d", "--dev-branch")
    parser.add_argument("-s", "--dev-suffix", default="dev")

    result = parser.parse_args(args)

    if result.dev_branch and not result.dev_suffix:
        parser.print_help()
        return None

    return result


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args:
        return 1

    versioner = FilamentVersioner(args.repository, args.main_branch)
    if not versioner.initialize():
        return 1

    if args.dev_branch:
        if not versioner.add_dev_tag(args.dev_branch, args.dev_suffix):
            return 1
    else:
        if not versioner.add_main_tag():
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
