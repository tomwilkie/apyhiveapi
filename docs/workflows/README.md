# GitHub Workflow Automation

How this repository's CI and releases run. Everything happens on `main`:
feature branches open pull requests against it, and a release is a version
bump merged into it.

---

## Branching model

```text
feature/* ──PR──▶ main ──version bump──▶ tag vX.Y.Z + GitHub Release
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
| `release.yml` | pushes to `main` that change `pyproject.toml` | Tags `v<version>`, builds the sdist and wheel, verifies the wheel imports, and publishes a GitHub Release with both attached. |

Dependabot (`.github/dependabot.yml`) opens weekly pull requests for pip
dependencies and GitHub Actions. Pre-commit hook revisions are updated with
`pre-commit autoupdate`.

---

## Releasing

1. Set `version` in `pyproject.toml` to a PEP 440 version such as `0.0.2` or
   `0.1.0b1`, in a pull request.
2. Merge it. `release.yml` finds no tag for that version, tags the merge
   commit, builds and verifies the distributions, and publishes a GitHub
   Release with generated notes and the sdist and wheel attached.
3. Point the Home Assistant fork at the wheel. In
   `custom_components/hive/manifest.json`:

   ```json
   "requirements": [
     "pyhive-integration @ https://github.com/tomwilkie/pyhive-integration/releases/download/v0.0.1/pyhive_integration-0.0.1-py3-none-any.whl"
   ]
   ```

A push to `main` that leaves the version alone does nothing, and a version
whose tag already exists is skipped with a log line, so merging a change to
`pyproject.toml` is always safe.

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
| No release after a merge | `pyproject.toml` did not change, or the version's tag already exists. Bump the version. |
| Release fails at "Verify the wheel" | The wheel is missing a module or data file: check `[tool.setuptools]` in `pyproject.toml` and `MANIFEST.in`. |
| Release fails creating the tag | The `GITHUB_TOKEN` needs `contents: write`, which the workflow requests; check the repository's Actions permissions allow it. |
| Home Assistant cannot install the requirement | The wheel filename in the manifest must match the release asset exactly, including the normalised version (`0.1.0b1`, not `0.1.0.B1`). |
