# Contributing

Issues and pull requests are welcome.

- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- Keep changes small and focused. Prefer a setting in the example config files over a new code path.
- Nothing specific to one company belongs in the code: project ids, URLs, Slack ids and emails go in config.
- Run the checks before opening a pull request:

  ```bash
  uv sync
  uv run ruff check . && uv run mypy src tests && uv run pytest
  ```

- Shell scripts: check them with `bash -n`, and run `deploy/gcp-setup.sh` with `DRY_RUN=1`.
- Never commit secrets, real env files or session pages.
- Report security issues privately through GitHub's "Report a vulnerability" on this repository, not in a public issue.

By contributing you agree that your contributions are licensed under the MIT License.
