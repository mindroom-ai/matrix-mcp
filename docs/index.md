---
icon: lucide/message-square-code
---

# Matrix MCP

**Local-first Matrix access for MCP clients**

<div style="text-align: center; margin: 1.5rem 0;">
  <img src="assets/logo.svg" alt="Matrix MCP logo" width="140" />
</div>

Matrix MCP lets Claude Code and other MCP clients inspect and participate in Matrix conversations from a local machine.
It is designed for workflows where a local coding agent should be able to read Matrix context, reply in threads, and attach local files without giving hosted agents access to the local filesystem.

[PyPI package](https://pypi.org/project/matrix-mcp/) · [GitHub repository](https://github.com/mindroom-ai/matrix-mcp)

## Quick Start

```bash
uv tool install matrix-mcp
matrix-mcp auth sso https://matrix.example.com
claude mcp add matrix -- matrix-mcp serve
```

For Codex, use:

```bash
codex mcp add matrix -- matrix-mcp serve
```

Continue with [Getting Started](getting-started.md), or see the [usage guide](usage.md) for the full tool surface.

## Features

- Matrix SSO, password auth, or existing access-token setup.
- Access-gateway support with static headers or command-generated headers.
- MCP tools for room listing, recent messages, thread reads, thread replies, and file attachments.
- Stable numeric refs for rooms and events so agents do not need to copy raw Matrix IDs between tool calls.
- Local credential storage in the user config directory.

## Tool Surface

| Tool | Purpose |
| --- | --- |
| `matrix_whoami` | Show the configured Matrix user and device. |
| `matrix_list_rooms` | List joined Matrix rooms visible to the authenticated user. |
| `matrix_read_room_recent` | Read recent text events from a room. |
| `matrix_read_thread` | Read a Matrix thread root and recent replies. |
| `matrix_send_message` | Send a text message or local file, optionally as a thread reply. |

## Numeric Refs

Read and list tools return stable numeric refs.
Use those refs in later tool calls instead of copying raw Matrix IDs:

```text
matrix_read_room_recent(room_id=1)
matrix_read_thread(room_id=1, thread_id=42)
matrix_send_message(room_id=1, body="reply", thread_id=42)
```
