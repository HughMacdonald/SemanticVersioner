# SemanticVersioner

A GitHub Action to increment the version based on conventional commits and whether we are in a dev or main branch.

## Description

SemanticVersioner is a GitHub Action that automates version incrementation based on [conventional commits](https://www.conventionalcommits.org) and the current branch.
It uses semantic versioning principles to determine the appropriate version bump. If it is being used on a development branch, it will apply a pre-release tag (`dev-suffix`) to the version.

If `include-shorter-versions` is set, it will also create (or move) shorter tags (e.g. `v1`, `v1.2`, `v1-dev`, etc) as well as creating the full version tag.

`preview-changelog` uses the same internal version and changelog logic as the mutating tagging flows, but does not create tags, commit, push, checkout branches, or write repository changelog files unless you explicitly ask it to write a standalone preview output file. It renders only the current changelog section for the computed version, not the full historical changelog file.

### Features

- Automatically increments version based on conventional commit messages
- Supports development and main branch versioning
- Supports non-mutating changelog preview mode for both main and dev flows
- Supports previewing an arbitrary target ref or commit
- Customizable development version suffix
- Option to include shorter version tags
- Emits machine-consumable outputs for preview workflows
- Configurable Python installation skip for environments with Python pre-installed

## Inputs

| Name | Description | Required | Default |
|---|---|---|---|
| `main-branch` | The main branch to use | No | `main` |
| `dev-branch` | The development branch to use | No | `""` |
| `dev-suffix` | The suffix to use for development versions | No | `dev` |
| `include-shorter-versions` | Include shorter versions of tags that move as new versions are created | No | `false` |
| `skip-python-install` | Skip the installation of Python if it is already available | No | `false` |
| `use-semantic-dev-versions` | Use semantic prerelease increments for dev versions | No | `false` |
| `changelog-file` | Repository changelog file to update during mutating flows | No | `""` |
| `changelog-message` | Optional message to prepend into the generated changelog | No | `""` |
| `dry-run` | Keep existing behavior and only calculate version outputs | No | `false` |
| `allow-tag-move` | Allow force-moving the unique full release tag if it already exists on the remote at a different commit. Does not affect `include-shorter-versions` alias tags, which always move | No | `false` |
| `stale-trigger` | How to handle a run whose checked-out HEAD no longer matches the live remote tip of the target branch: `ignore`, `skip`, or `fail` | No | `ignore` |
| `preview-changelog` | Generate the current changelog section and version outputs without mutating git state | No | `false` |
| `target-ref` | Ref or commit to preview instead of the current branch head. Only used with `preview-changelog` | No | `""` |
| `print-changelog` | Print the rendered preview changelog markdown to stdout | No | `false` |
| `preview-output-file` | Write the rendered preview changelog markdown to a file | No | `""` |

## Outputs

| Name | Description |
|---|---|
| `previous-version` | The version before the version was incremented |
| `new-version` | The new version after the version was incremented |
| `rendered-changelog` | The rendered current changelog section produced by preview mode |

## Usage

On push to `main`:

```yaml
- name: Increment Version
  uses: hughmacdonald/semantic-versioner@v1
  with:
    main-branch: main
    include-shorter-versions: true
    skip-python-install: false
```

On push to `develop`:

```yaml
- name: Increment Version
  uses: hughmacdonald/semantic-versioner@v1
  with:
    main-branch: main
    dev-branch: develop
    dev-suffix: dev
    include-shorter-versions: true
    skip-python-install: false
```

Preview what would be produced for `main` without creating tags or writing files:

```yaml
- name: Preview Main Release
  id: preview
  uses: hughmacdonald/semantic-versioner@v1
  with:
    main-branch: main
    preview-changelog: true
    print-changelog: true
```

Preview what would be produced for `develop` against an arbitrary merge ref:

```yaml
- name: Preview Develop Release
  id: preview
  uses: hughmacdonald/semantic-versioner@v1
  with:
    main-branch: main
    dev-branch: develop
    dev-suffix: dev
    use-semantic-dev-versions: true
    preview-changelog: true
    target-ref: refs/pull/${{ github.event.pull_request.number }}/merge
```

## Pull Request Preview Example

This workflow fetches a PR merge ref, runs SemanticVersioner in changelog preview mode against it, captures the rendered current changelog section, and creates or updates a PR comment.

```yaml
name: Preview Release Notes

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  preview:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Fetch PR merge ref
        run: git fetch origin refs/pull/${{ github.event.pull_request.number }}/merge:refs/pull/${{ github.event.pull_request.number }}/merge

      - name: Preview version and changelog
        id: preview
        uses: hughmacdonald/semantic-versioner@v1
        with:
          main-branch: main
          dev-branch: develop
          dev-suffix: dev
          use-semantic-dev-versions: true
          preview-changelog: true
          target-ref: refs/pull/${{ github.event.pull_request.number }}/merge
          preview-output-file: semantic-version-preview.md

      - name: Comment on PR
        uses: actions/github-script@v7
        env:
          PREVIOUS_VERSION: ${{ steps.preview.outputs.previous-version }}
          NEW_VERSION: ${{ steps.preview.outputs.new-version }}
          CHANGELOG: ${{ steps.preview.outputs.rendered-changelog }}
        with:
          script: |
            const marker = "<!-- semantic-versioner-preview -->";
            const body = `${marker}
            SemanticVersioner preview

            Previous version: ${process.env.PREVIOUS_VERSION}
            New version: ${process.env.NEW_VERSION}

            ${process.env.CHANGELOG}`;

            const { data: comments } = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
            });

            const existing = comments.find(comment => comment.body.includes(marker));

            if (existing) {
              await github.rest.issues.updateComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                comment_id: existing.id,
                body,
              });
            } else {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body,
              });
            }
```

## Concurrency and single-writer assumption

For any given run, this action assumes it is the **sole writer** of the target branch and of the
release tags it creates. It commits the changelog to the branch, pushes it, and only then tags
the pushed commit. It does not lock the branch or coordinate with other runs.

If two runs execute concurrently — for example duplicate `push` webhook deliveries, which are
outside the caller's control, both checked out at the same triggering sha (`actions/checkout`
uses `github.sha`, not the live branch tip) — one run will win the branch push and the other's
push will be a non-fast-forward. To keep this safe, the action guarantees:

- **A rejected branch push is fatal.** If the changelog branch push is rejected (detected via
  git's structured push status, not by scraping stderr), the run logs an error naming the
  rejected ref and exits non-zero **before creating or pushing any tag**. A tag is never created
  for a commit that is not present on the remote.
- **The unique release tag is never moved or deleted destructively.** The full version tag
  (e.g. `v1.2.3`, `v1.38.0-dev.0.3.1`) is published with an explicit
  `git push origin refs/tags/<tag>` (never `--tags`, never `--force`). If that tag already
  exists on the remote:
  - pointing at the **same** commit, the run treats it as an idempotent success;
  - pointing at a **different** commit, the run **fails** with a message naming both the existing
    and the attempted commit, and leaves the published tag untouched (unless `allow-tag-move`
    is `true`, an explicit escape hatch for the rare case where you genuinely need to move it).
- **Rolling alias tags still move.** The shorter alias tags produced by `include-shorter-versions`
  (e.g. `v1`, `v1.2`, `v1-dev`) are meant to advance to each release, so they are moved to the new
  commit automatically. This needs no configuration and is unaffected by `allow-tag-move`.
- **Optional stale-trigger detection.** Set `stale-trigger` to `skip` or `fail` to have the run
  compare its checked-out HEAD against the live remote tip of the target branch before doing any
  work, and bail out (successfully or non-zero, respectively) if it is operating on a superseded
  commit.

### Failure modes

| Situation | Behaviour |
|---|---|
| Branch push rejected (non-fast-forward) | Log error naming the ref, exit non-zero, no tag created |
| Release tag already on the same commit | Idempotent success, no change |
| Release tag already on a different commit, `allow-tag-move: false` | Fail with both shas, tag left untouched |
| Release tag already on a different commit, `allow-tag-move: true` | Release tag force-moved to the new commit |
| Rolling alias tag (`include-shorter-versions`) on an older commit | Moved to the new commit automatically |
| HEAD behind live branch tip, `stale-trigger: skip` | Exit `0` without mutating anything |
| HEAD behind live branch tip, `stale-trigger: fail` | Exit non-zero without mutating anything |

## Behaviour changes

A correct single-writer run is unaffected by these fixes — it produces the same commits, tags,
and outputs as before. The differences only surface in the failure and concurrency cases:

- A rejected branch push is now fatal (previously it was logged as a warning and the run
  continued and exited success on an invalid state).
- The unique release tag is pushed non-destructively and is never deleted/recreated; a
  conflicting remote release tag now fails the run instead of being silently repointed.
- The rolling alias tags from `include-shorter-versions` (e.g. `v1`, `v1.2`, `v1-dev`) continue
  to move to each release automatically, exactly as before — no configuration change is needed.

## Author

Hugh Macdonald
