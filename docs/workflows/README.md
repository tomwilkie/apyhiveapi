# GitHub Workflow Automation

How this repository's CI and releases run. Everything happens on `main`:
feature branches open pull requests against it, and every merge into it is a
release.

---

## Branching model

```text
feature/* ──PR──▶ main ──patch bump──▶ tag vX.Y.Z + GitHub Release
```

- Work happens on feature branches; pull requests target `main`.
- `main` is protected: pull requests need the CI checks below to pass.
- There is no `dev` or `master` branch and no separate release branch.

---

## Workflows

| Workflow | Triggers | What it does |
|---|---|---|
| `ci.yml` (Lint) | pull requests and pushes to `main` that touch code or lint config | Runs every pre-commit hook: ruff, pylint, mypy, bandit, codespell, yamllint, detect-secrets and the PII check on fixture data. |
| `tests.yml` (Tests, Coverage, Packaging) | every pull request and push to `main` | Tests on Python 3.13 and 3.14, the 99% coverage gate, and a wheel built and imported in a clean venv. The jobs always report, so they can be required checks. |
| `claude.yml` | pull requests and `@claude` mentions | Claude reviews the change against `CLAUDE.md` and the Home Assistant fork. |
| `release.yml` | every push to `main` | Bumps the patch version unless the pull request already set a new one, tags `v<version>`, builds the sdist and wheel, verifies the wheel imports, and publishes a GitHub Release with both attached. |

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for pip
dependencies and GitHub Actions. Pre-commit hook revisions are updated with
`pre-commit autoupdate`.

---

## Releasing

Merging a pull request is releasing. On every push to `main`, `release.yml`
checks out the tip of `main` and reads `version` from `pyproject.toml`:

- **The version is already tagged** (the usual case: the pull request left it
  alone). The patch number is bumped, `0.1.4` → `0.1.5`, and the bump is
  committed to `main` as `github-actions[bot]` with the message
  `Release v0.1.5`. That commit is tagged and released.
- **The version is not tagged yet** (the pull request set it, for a minor or
  major bump such as `0.2.0`, or a pre-release such as `0.2.0b1`). It is
  tagged and released as it is, on the merge commit.

Either way the sdist and wheel are built, the wheel is checked to import in a
clean venv, and a GitHub Release is published with generated notes and both
files attached.

Point the Home Assistant fork at the wheel. In
`custom_components/hive/manifest.json`:

```json
"requirements": [
  "https://github.com/tomwilkie/pyhive-integration/releases/download/v0.1.4/pyhive_integration-0.1.4-py3-none-any.whl#pyhive-integration==0.1.4"
]
```

Home Assistant reads the `#pyhive-integration==<version>` fragment to tell
whether that version is already installed; without it, the wheel is
reinstalled on every start.

Details worth knowing:

- Dependabot and docs-only merges are releases too. A release with no library
  change is harmless; the fork only picks up a version when its manifest is
  bumped to it.
- The bump commit is pushed with `GITHUB_TOKEN`, which triggers no workflows,
  so Lint and Tests do not run on it and it cannot start another release. A
  tip of `main` authored by `github-actions[bot]` is skipped anyway, so the
  workflow cannot loop even if that changes.
- Runs are serialised (`concurrency: release`). Two merges in quick
  succession give one release containing both when the first run checks out
  a tip that already includes the second merge; the second run then finds the
  bot's commit at the tip and does nothing.
- The patch bump only reads the leading `major.minor.patch` of the version, so
  a tagged pre-release such as `0.2.0b1` is followed by `0.2.1`. Set the
  final version in a pull request instead when that matters.
- `main` must let `github-actions[bot]` push directly. If branch protection or
  a ruleset is added, give the Actions app a bypass.

Releases are not published to PyPI: the `pyhive-integration` name there
belongs to the upstream project. To publish under a name of your own, change
`project.name` in `pyproject.toml`, register a trusted publisher for that name
on PyPI pointing at this repository and `release.yml`, and add a job to
`release.yml` that downloads the built distributions and runs
`pypa/gh-action-pypi-publish` with `id-token: write`.

---

## Required status checks on `main`

- `Lint`
- `Tests (Python 3.13)`, `Tests (Python 3.14)`, `Coverage` and `Packaging`

The Tests workflow has no path filter and reports every job even when a
change touches no Python, so the checks never block a docs-only pull request.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No release after a merge | The tip of `main` was authored by `github-actions[bot]`, or an earlier run released the merge together with a previous one. Check the Release workflow's log. |
| Release fails pushing the bump | `main` rejects pushes from the Actions app (branch protection or a ruleset without a bypass), or the `version` line in `pyproject.toml` is not `version = "<major>.<minor>.<patch>"`. |
| Release fails at "Verify the wheel" | The wheel is missing a module or data file: check `[tool.setuptools]` in `pyproject.toml` and `MANIFEST.in`. |
| Release fails creating the tag | The `GITHUB_TOKEN` needs `contents: write`, which the workflow requests; check the repository's Actions permissions allow it. |
| Home Assistant cannot install the requirement | The wheel filename in the manifest must match the release asset exactly, including the normalised version (`0.1.0b1`, not `0.1.0.B1`). |
