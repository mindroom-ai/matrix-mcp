# Matrix MCP

Matrix MCP is a local-first MCP server that lets Claude Code and other MCP clients read and write Matrix rooms.
It is intended to make MindRoom conversations available to local coding agents without giving hosted agents access to the local filesystem.

## Install

```bash
uv tool install git+https://github.com/mindroom-ai/matrix-mcp
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
- `matrix_send_message`: send a text message, optionally as a Matrix thread reply.

The tool instructions tell clients to prefer read tools first and only send messages when the user explicitly asks.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv build
```
