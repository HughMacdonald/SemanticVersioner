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

## Author

Hugh Macdonald
