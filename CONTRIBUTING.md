# Contributing

Bug reports, ideas and pull requests are all welcome. For anything big, open an issue first so we can agree on the approach.

## Setup

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/zaman-labs/fastapi-panel
cd fastapi-panel
uv sync
uv run pytest
```

Before pushing:

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
```

To see your changes in a browser, run the [example shop](examples/shop). To check the interface end to end and refresh the screenshots, keep the example running on port 8765 and run:

```bash
uv run --with playwright python scripts/screenshots.py
```

## Branches

- `main` is the only long-lived branch and always stable. Open pull requests against it.
- Name your branch `feature/<name>`, `fix/<name>` or `docs/<name>`.

To release, commit `chore: release X.Y.Z` on `main` that bumps the version in `src/fastapi_panel/__init__.py` and turns `[Unreleased]` in `CHANGELOG.md` into `[X.Y.Z] - date`. Publish a GitHub release tagged `vX.Y.Z` with that changelog section, then upload to PyPI by running the publish workflow on the tag: `gh workflow run publish.yml --ref vX.Y.Z`.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat: add radio fields`, `fix: keep filters after saving`, `docs: explain inlines`. Keep each commit about one thing.

## What we look for

- **Small and readable.** This library should stay easy to read in an afternoon. Prefer fewer options and plain code over flexibility nobody needs yet.
- **Consistent.** New options follow the naming of the existing ones.
- **Tested.** New behavior and bug fixes come with a test. Security behavior always does.
- **Secure by default.** No inline scripts or styles, and no new way to write a field that isn't on the form. Anything that touches login, sessions or permissions gets extra attention.
- **No new dependencies** unless there's really no other way.

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Code of conduct

Everyone taking part agrees to our [Code of Conduct](CODE_OF_CONDUCT.md).
