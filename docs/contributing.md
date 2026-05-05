---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome.

## Development Setup

```bash
git clone https://github.com/mindroom-ai/matrix-mcp.git
cd matrix-mcp
uv sync --extra dev --group docs
```

## Run Tests

```bash
uv run pytest
```

## Code Quality

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run ty check
uv build
```

The repository also uses pre-commit:

```bash
uv run prek run --all-files
```

## Build Docs

```bash
uv run zensical build
```

The generated site is written to `site/`.

## Project Structure

```text
src/matrix_mcp/
├── auth.py           Matrix login and SSO callback handling
├── cli.py            Typer CLI
├── config.py         Stored credentials and config paths
├── http_headers.py   Static and command-generated HTTP headers
├── id_state.py       Stable numeric refs for rooms and events
├── matrix_client.py  Matrix client wrapper
└── mcp_server.py     FastMCP tool registration
```

## Release

Releases are published from GitHub Releases through trusted publishing.
Create a release tag such as `v0.4.0`; the `release.yml` workflow builds and uploads to PyPI.
