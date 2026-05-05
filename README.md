# Matrix MCP

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Python Versions](https://img.shields.io/pypi/pyversions/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![MCP](https://img.shields.io/badge/MCP-server-blue)](https://modelcontextprotocol.io/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Local-first Matrix access for MCP clients.

Matrix MCP lets Claude Code and other MCP clients read and write Matrix rooms.
It is intended to make MindRoom conversations available to local coding agents without giving hosted agents access to the local filesystem.

## Install

```bash
uv tool install matrix-mcp
```

For local development:

```bash
uv sync --extra dev
```

## Login

Matrix SSO:

```bash
matrix-mcp auth sso https://matrix.example.com
```

If the homeserver advertises multiple SSO providers, list their provider IDs:

```bash
matrix-mcp auth providers https://matrix.example.com
```

Then pass the provider ID explicitly:

```bash
matrix-mcp auth sso https://matrix.example.com --idp-id github
```

Existing Matrix access token:

```bash
matrix-mcp auth token https://matrix.example.com @alice:example.com "$MATRIX_ACCESS_TOKEN" --device-id DEVICEID
```

Password auth, when enabled by the homeserver:

```bash
matrix-mcp auth password https://matrix.example.com @alice:example.com
```

Credentials are stored in the user config directory reported by:

```bash
matrix-mcp config-path
```

Remove stored credentials:

```bash
matrix-mcp auth logout
```

## Claude Code

Add the local MCP server:

```bash
claude mcp add matrix -- matrix-mcp serve
```

The server runs over stdio. It does not expose a local HTTP port during normal MCP operation.

## Tools

- `matrix_whoami`: show the configured Matrix user/device.
- `matrix_list_rooms`: list rooms joined by the authenticated user.
- `matrix_read_room_recent`: read recent text events from a room.
- `matrix_read_thread`: read a Matrix thread root and its recent text replies.
- `matrix_send_message`: send a text message or local file, optionally as a Matrix thread reply.

The tool instructions tell clients to prefer read tools first and only send messages when the user explicitly asks.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy src tests
uv run --extra dev ty check
uv build
```
