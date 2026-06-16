# Contributing to modpoll2mqtt

Contributions are welcome. Every little bit helps, and credit will always be given.

## Types of Contributions

### Report Bugs

Report bugs at [https://github.com/yoch/modpoll2mqtt/issues](https://github.com/yoch/modpoll2mqtt/issues).

When reporting a bug, please include:

- Your operating system name and version.
- Any details about your local setup that might be helpful in troubleshooting.
- Detailed steps to reproduce the bug.

### Fix Bugs

Look through the GitHub issues for bugs. Anything tagged with "bug" and "help wanted" is open for anyone to fix.

### Implement Features

Look through the GitHub issues for features. Anything tagged with "enhancement" and "help wanted" is open for anyone to implement.

### Write Documentation

`modpoll2mqtt` could always use more documentation, whether as part of the official docs, in docstrings, or even on the web in blog posts, articles, and such.

### Submit Feedback

The best way to send feedback is to file an issue at [https://github.com/yoch/modpoll2mqtt/issues](https://github.com/yoch/modpoll2mqtt/issues).

If you are proposing a new feature:

- Explain in detail how it would work.
- Keep the scope as narrow as possible to make it easier to implement.
- Remember that this is a volunteer-driven project, and contributions are welcome.

## Get Started!

Ready to contribute? Here's how to set up the project for local development.

Here we assume you already have `python`, `poetry`, and `Git` installed. Otherwise, you can use the [asdf](https://github.com/asdf-vm/asdf) tool to manage the required tools or install them manually according to the [`.tool-versions`](.tool-versions) file (Python 3.12+, Poetry 2.4.x, pandoc 3.10).

On Linux with KDE Wallet or other desktop keyrings, disable Poetry's keyring integration to avoid spurious authorization prompts:

```bash
poetry config keyring.enabled false
```

1. Fork the `modpoll2mqtt` repo on GitHub, and then clone your fork locally,

    ```bash
    git clone https://github.com/your-username/modpoll2mqtt.git
    cd modpoll2mqtt
    ```

2. Install and activate the dev environment,

    ```bash
    make install
    source .venv/bin/activate
    ```

3. Create a branch for local development,

    ```bash
    git checkout -b your-branch-name
    ```

    Now you can make your changes locally.

4. Don't forget to add test cases for your added functionality in the `tests` directory.

5. **Before every `git commit`** (not only before a release or pull request), run the
   same quality checks as CI (`ci-check` workflow in
   [`.github/workflows/ci-check.yml`](.github/workflows/ci-check.yml)):

    ```bash
    make check
    ```

    This runs `black` among other tools. **Do not commit Python changes until
    `make check` passes cleanly** — if `black` reformats files, stage those changes and
    include them in the same commit. Committing first and amending afterward is a smell;
    it usually means this step was skipped.

    `make install` registers pre-commit hooks that catch many issues at commit time, but
    they do not replace a full `make check` (deptry, poetry lock, etc.). Re-run
    `make check` after fixing hook failures.

    Then, validate that all unit tests are passing:

    ```bash
    make test
    ```

    For the full local CI gate (same as pull request checks):

    ```bash
    make ci
    ```

    Integration tests are optional and split by dependency:

    ```bash
    make test-integration-modbus   # auto-starts a local Modbus TCP simulator on port 1502 if needed
    make test-integration-mqtt     # requires a broker (default broker.emqx.io)
    make test-integration          # both
    ```

    If Modbus integration tests skip, start the simulator manually:

    ```bash
    poetry run python tests/modbus_integration.py
    ```

    Override endpoints with `MODBUS_TEST_HOST` / `MODBUS_TEST_PORT` or
    `MQTT_TEST_HOST` / `MQTT_TEST_PORT` when needed.

6. Commit your changes and push your branch to GitHub. Run `make check` again if you
   changed files after the last successful run:

    ```bash
    make check
    git add .
    git commit -m "Your detailed description of your changes."
    git push origin your-branch-name
    ```

    Please note this project follows [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) specification for git commits.

7. Submit a pull request through the GitHub website.

## Releasing

PyPI publish and the public documentation site are **not** updated on ordinary pushes to `main`.
They run only when:

1. A **version tag** `vX.Y.Z` is pushed, or a **GitHub Release is published** — both trigger [`.github/workflows/on-release-main.yml`](.github/workflows/on-release-main.yml), or
2. That workflow is started **manually** from the Actions tab (`workflow_dispatch`).

The workflow publishes the package to PyPI (trusted publisher / OIDC) and deploys Sphinx output to the `gh-pages` branch.

### Documentation checklist (required before every release)

There is no automated doc gate in CI. **Agents and maintainers must verify
documentation manually** whenever code or CLI options change — especially before
tagging a release.

1. **`CHANGELOG.md`** — add or move entries under `[Unreleased]`; each new CLI
   flag introduced with `add \`--flag\`` in the Features section.
2. **`docs/changelog.rst`** — regenerate from `CHANGELOG.md` (`make docs-changelog`
   or `make docs`); commit the updated file with the release commit.
3. **Narrative docs** — keep `README.md`, `docs/quickstart.rst`, and
   `docs/usage.rst` aligned with current behavior:
   - every new user-facing CLI flag is mentioned in at least one of these files;
   - MQTT write examples use a **reference map** (`{"ref_name": val}`), not the
     deprecated `ref`/`value` object format removed in 2.1.0;
   - breaking changes are called out with the version they shipped in.
4. **`docs/usage.rst`** — keep the `.. argparse::` directive pointing at
   `modpoll.arg_parser.get_parser` so the CLI reference stays auto-generated.
5. **Smoke build** — run `make docs` locally and fix any Sphinx errors before
   pushing the tag.
6. **Live site check** — after the `release-main` workflow completes, confirm
   [yoch.github.io/modpoll2mqtt](https://yoch.github.io/modpoll2mqtt/) shows the
   new changelog version and CLI options (not only that the `gh-pages` branch
   was updated). If the site is stale, check **Settings → Pages → Build status**
   for a failed Pages build; redeploy with **Actions → deploy-docs → Run workflow**.
7. **Quality gate** — run the same checks as CI before tagging:

   ```bash
   make ci
   ```

   If `make check` reformats files (e.g. `black`), stage the fixes and amend or
   recommit before pushing.

### Typical release flow

```bash
# 1. bump version in pyproject.toml
# 2. finalize CHANGELOG.md (move [Unreleased] → [X.Y.Z])
# 3. complete the documentation checklist above
make docs-changelog
make ci
git tag vX.Y.Z
# after push + release workflow: verify https://yoch.github.io/modpoll2mqtt/changelog.html
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file CHANGELOG-excerpt.md
```

**Manual re-run** (same version already on PyPI will fail at upload; use only to retry a failed docs deploy after fixing the workflow):

```bash
gh workflow run release-main --repo yoch/modpoll2mqtt --ref main
```

CI on push runs two workflows — `ci-check` (lint + docs build) and `ci-test` — and does not deploy the site.

Happy Coding!
