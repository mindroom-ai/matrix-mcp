# Matrix MCP

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Python Versions](https://img.shields.io/pypi/pyversions/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Docs](https://img.shields.io/badge/docs-matrix--mcp.mindroom.chat-blue)](https://matrix-mcp.mindroom.chat/)
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
matrix-mcp auth sso https://mindroom.chat
```

If the homeserver advertises multiple SSO providers, list their provider IDs:

```bash
matrix-mcp auth providers https://mindroom.chat
```

Then pass the provider ID explicitly:

```bash
matrix-mcp auth sso https://mindroom.chat --idp-id github
```

The SSO flow starts a temporary callback server on the machine running `matrix-mcp` and waits for the browser to be redirected to it.
If that machine is remote (for example over SSH), a browser on your local machine cannot reach the callback.
Pin the callback port and forward it from the machine with your browser:

```bash
# on the remote machine
matrix-mcp auth sso https://mindroom.chat --callback-port 8765
```

```bash
# on your local machine, in a second terminal
ssh -N -L 8765:127.0.0.1:8765 remote-host
```

Then open the printed SSO URL in your local browser.
After login, the homeserver redirects to `http://127.0.0.1:8765/callback`, which SSH forwards to the waiting command on the remote machine.
If port forwarding is not an option, use the manual flow described in the
[getting started guide](https://matrix-mcp.mindroom.chat/getting-started/) with
`matrix-mcp auth sso-url` and `matrix-mcp auth login-token`.

If your homeserver is behind an access gateway that requires extra request headers, pass them during login.
They are stored with the Matrix credentials and reused by MCP tools:

```bash
matrix-mcp auth sso https://mindroom.chat \
  --header "X-Access-Client-Id: ..." \
  --header "X-Access-Client-Secret: ..."
```

If the gateway header is short-lived, store a command that prints the current header value instead.
The command is re-run when Matrix MCP creates a client for tool calls:

```bash
matrix-mcp auth sso https://mindroom.chat \
  --header-command "X-Access-Token: access-gateway-cli token --app https://mindroom.chat"
```

For homeservers behind Cloudflare Access, `matrix-mcp` can configure the dynamic
`cf-access-token` header for you. This uses the local `cloudflared` CLI to log in
when needed during setup, then stores a command that reads the current token:

```bash
brew install cloudflared
matrix-mcp auth sso https://mindroom.chat --cloudflare-access
```

For other platforms, install `cloudflared` from Cloudflare's downloads page.

Existing Matrix access token:

```bash
matrix-mcp auth token https://mindroom.chat @alice:mindroom.chat "$MATRIX_ACCESS_TOKEN" --device-id DEVICEID
```

Password auth, when enabled by the homeserver:

```bash
matrix-mcp auth password https://mindroom.chat @alice:mindroom.chat
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

## Codex

Add the local MCP server:

```bash
codex mcp add matrix -- matrix-mcp serve
```

The server runs over stdio. It does not expose a local HTTP port during normal MCP operation.

## Tools

- `matrix_whoami`: show the configured Matrix user/device.
- `matrix_list_rooms`: list rooms joined by the authenticated user.
- `matrix_read_room_recent`: read recent text events from a room.
- `matrix_read_thread`: read a Matrix thread root and its recent text replies.
- `matrix_send_message`: send a text message or local file, optionally as a Matrix thread reply.

Rooms and events returned by read/list tools include stable numeric `id` fields.
Thread replies also include `thread_ref`, which is the numeric event ref of the thread root.
Use these integers in later tool calls instead of copying raw Matrix IDs:

```text
matrix_read_room_recent(room_id=1)
matrix_read_thread(room_id=1, thread_id=42)
matrix_send_message(room_id=1, body="reply", thread_id=42)
```

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
