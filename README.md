# SemanticVersioner

A GitHub Action to increment the version based on conventional commits and whether we are in a dev or main branch.

## Description

SemanticVersioner is a GitHub Action that automates version incrementation based on [conventional commits](https://www.conventionalcommits.org) and the current branch.
It uses semantic versioning principles to determine the appropriate version bump. If it is being used on a development branch, it will apply a pre-release tag (`dev-suffix`) to the version.

If `include-shorter-versions` is set, it will also create (or move) shorter tags (e.g. `v1`, `v1.2`, `v1-dev`, etc) as well as creating the full version tag.

### Features

- Automatically increments version based on conventional commit messages
- Supports development and main branch versioning
- Customizable development version suffix
- Option to include shorter version tags
- Configurable Python installation skip for environments with Python pre-installed

## Inputs

| Name                       | Description                                                             | Required | Default |
|----------------------------|-------------------------------------------------------------------------|----------|---------|
| `main-branch`              | The main branch to use                                                  | No       | `main`  |
| `dev-branch`               | The development branch to use (if required). If this is not specified,  | No       | `""`    |
| `dev-suffix`               | The suffix to use for development versions                              | No       | `dev`   |
| `include-shorter-versions` | Include shorter versions of tags that move as new versions are created  | No       | `false` |
| `skip-python-install`      | Skip the installation of Python (if it's already installed)             | No       | `false` |

## Outputs

| Name               | Description                                         |
|--------------------|-----------------------------------------------------|
| `previous-version` | The version before the version was incremented.     |
| `new-version`      | The new version after the version was incremented.  |

## Usage

On push to `main`:

    - name: Increment Version
      uses: hughmacdonald/semantic-versioner@v1
      with:
        main-branch: main
        include-shorter-versions: true
        skip-python-install: false

On push to `develop`:

    - name: Increment Version
      uses: hughmacdonald/semantic-versioner@v1
      with:
        main-branch: main
        dev-branch: develop
        dev-suffix: dev
        include-shorter-versions: true
        skip-python-install: false

## Author

Hugh Macdonald
