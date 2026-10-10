---
icon: lucide/git-pull-request
---

# Contributing

Contributions are welcome, from typo fixes to new tools.
Open an [issue](https://github.com/mindroom-ai/matrix-mcp/issues) to report a bug or discuss an idea before a larger change.

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

Two opt-in tests run against a real homeserver: `tests/test_tools_live.py` exercises the room, search, and moderation tools, and `tests/test_e2ee_live.py` exercises encrypted rooms.
Point them at a disposable homeserver that allows registration:

```bash
MATRIX_MCP_LIVE_HOMESERVER=http://127.0.0.1:8008 \
MATRIX_MCP_LIVE_REGISTRATION_TOKEN=... uv run pytest tests/test_tools_live.py tests/test_e2ee_live.py
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
uv run zensical serve   # live preview at http://localhost:8000
uv run zensical build   # static site in site/
```

## Project Structure

```text
src/matrix_mcp/
├── auth.py                Matrix login and SSO callback handling
├── cli.py                 Typer CLI
├── config.py              Stored credentials and config paths
├── conversation_tools.py  History, message, membership, media, and catch-up tools
├── e2ee.py                End-to-end encryption for the local device
├── hosted_auth.py         OAuth for authenticated HTTP mode
├── hosted_server.py       Authenticated HTTP tool registration
├── http_headers.py        Static and command-generated HTTP headers
├── id_state.py            Stable numeric refs for rooms and events
├── matrix_client.py       Matrix client wrapper
├── matrix_events.py       History, search, threads, reactions, and event sends
├── matrix_http.py         Bounded Matrix HTTP requests
├── matrix_media.py        Media upload and download
├── matrix_moderation.py   Kicks, bans, power levels, and pins
├── matrix_rooms.py        Invitations, direct chats, spaces, receipts, and unread catch-up
├── mcp_server.py          Local (stdio) tool registration
└── tls.py                 TLS defaults
```

## Release

Releases are published from GitHub Releases through trusted publishing.
Create a release tag such as `v0.4.0`; the `release.yml` workflow builds and uploads to PyPI.
