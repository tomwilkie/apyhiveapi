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
| `release.yml` | every push to `main`, and by hand for a minor or major release | Bumps the version with `bump-my-version`, builds the sdist and wheel, verifies the wheel imports, pushes the bump commit and its `v<version>` tag to `main`, and publishes a GitHub Release with both files attached. |

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for pip
dependencies and GitHub Actions. Pre-commit hook revisions are updated with
`pre-commit autoupdate`.

---

## Releasing

Merging a pull request is releasing. Pull requests leave `version` in
`pyproject.toml` alone. On every push to `main`, `release.yml` checks out the
tip of `main` and runs `bump-my-version bump patch`, which rewrites `version`
(`0.1.4` → `0.1.5`), commits it as `github-actions[bot]` with the message
`Release v0.1.5 [skip release]` and tags the commit `v0.1.5`. The job then
builds the sdist and wheel, checks that the wheel imports in a clean venv,
pushes the commit and tag to `main`, and publishes a GitHub Release with
generated notes and both files attached. Nothing lands on `main` unless the
wheel built.

For a **minor or major release**, run the workflow by hand: Actions →
Release → Run workflow, and pick `minor` or `major` as the part to bump. The
same job runs with that part.

`bump-my-version` is configured in `[tool.bumpversion]` in `pyproject.toml`
and reads the current version from `project.version`, so the version is
written in one place only.

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
  so Lint and Tests do not run on it and it cannot start another release. Its
  `[skip release]` marker makes the job skip itself anyway, so the workflow
  cannot loop even if that changes.
- Runs are serialised (`concurrency: release`). Two merges in quick
  succession give one release containing both when the first run checks out
  a tip that already includes the second merge; the second run then bumps
  again and publishes a release with no library change in it.
- `bump-my-version` is installed unpinned in the release job, like the other
  development tools in this repository. It has write access to `main` through
  the job's token, so pin it if that trade-off changes.
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
| No release after a merge | The merge commit's message contains `[skip release]`, or an earlier run released the merge together with a previous one. Check the Release workflow's log. |
| Release fails at "Bump the version" | `bump-my-version` could not parse `project.version` or find `version = "<current>"` in `pyproject.toml`; check `[tool.bumpversion]` there. |
| Release fails pushing the bump | `main` rejects pushes from the Actions app (branch protection or a ruleset without a bypass). The tag and commit exist only in the runner, so nothing needs cleaning up; fix the protection and re-run. |
| Release fails at "Verify the wheel" | The wheel is missing a module or data file: check `[tool.setuptools]` in `pyproject.toml` and `MANIFEST.in`. |
| Release fails creating the tag | The `GITHUB_TOKEN` needs `contents: write`, which the workflow requests; check the repository's Actions permissions allow it. |
| Home Assistant cannot install the requirement | The wheel filename in the manifest must match the release asset exactly, including the normalised version (`0.1.0b1`, not `0.1.0.B1`). |
