# Matrix MCP

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Python Versions](https://img.shields.io/pypi/pyversions/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Docs](https://img.shields.io/badge/docs-matrix--mcp.mindroom.chat-blue)](https://matrix-mcp.mindroom.chat/)
[![MCP](https://img.shields.io/badge/MCP-server-blue)](https://modelcontextprotocol.io/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/matrix-mcp/main/docs/assets/logo.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/matrix-mcp/main/docs/assets/logo-static.svg" alt="Matrix MCP logo" align="right" width="120" />
</picture>

Local-first Matrix access for MCP clients.

Matrix MCP lets Claude Code and other MCP clients read and write Matrix rooms.
It is intended to make MindRoom conversations available to local coding agents without giving hosted agents access to the local filesystem.

For remote clients, opt into [authenticated HTTP](https://matrix-mcp.mindroom.chat/hosted/), where each caller connects their own Matrix account through browser SSO.

**Documentation:** [matrix-mcp.mindroom.chat](https://matrix-mcp.mindroom.chat/)

## Install

```bash
uv tool install matrix-mcp
```

## Login

```bash
matrix-mcp auth sso https://mindroom.chat
```

[Getting Started](https://matrix-mcp.mindroom.chat/getting-started/) covers choosing an SSO provider, logging in over SSH or on a headless machine, access tokens, and passwords.
For homeservers behind access gateways such as Cloudflare Access, see [Access Gateways](https://matrix-mcp.mindroom.chat/usage/#access-gateways).
`matrix-mcp auth logout` removes the stored credentials.

## Connect an MCP Client

```bash
claude mcp add matrix -- matrix-mcp serve   # Claude Code
codex mcp add matrix -- matrix-mcp serve    # Codex
```

The server runs over stdio and does not open a local HTTP port.

## Encrypted Rooms

In local mode, Matrix MCP is its own encrypted Matrix device: encrypted rooms work like any other room.
Each login publishes the device's keys; messages from before the login need keys imported with `matrix-mcp e2ee import-keys`.
See [End-to-End Encryption](https://matrix-mcp.mindroom.chat/encryption/) for details and limitations.

## Tools

- `matrix_whoami`: show the configured Matrix user/device.
- `matrix_list_rooms`: list rooms joined by the authenticated user.
- `matrix_read_room_recent`: read recent messages and attachments from a room.
- `matrix_read_thread`: read a Matrix thread root and its recent message replies.
- `matrix_send_message`: send a text message or local file, optionally as a Matrix thread reply.
- `matrix_list_room_members`: page through joined members and their profiles.
- `matrix_search_users`: find user IDs in the homeserver's visible user directory.
- `matrix_invite_user`: invite a user to a room.
- `matrix_get_room_info`: read a room's name, topic, and avatar.
- `matrix_set_room_name`, `matrix_set_room_topic`, `matrix_set_room_avatar`: update room details.
- `matrix_get_profile`: look up your own or another user's profile.
- `matrix_set_display_name`, `matrix_set_avatar`: update your own global profile.
- `matrix_read_history`, `matrix_get_event_context`: page through history and inspect a message's context.
- `matrix_reply`, `matrix_react`: reply to a specific event or add a reaction.
- `matrix_edit_message`, `matrix_redact_event`: correct your messages or remove your event content, including reactions.
- `matrix_list_invitations`, `matrix_join_room`, `matrix_leave_room`, `matrix_create_room`: manage your room membership and create private rooms.
- `matrix_upload_media`, `matrix_download_media`, `matrix_send_media`: transfer bounded files and images using Matrix media URIs; pass the message's `room_id` and `event_id` to download an encrypted attachment.
- `matrix_get_unread`, `matrix_mark_read`: inspect unread activity and explicitly update read markers.

All actions use the connected account's Matrix permissions.
See the [usage guide](https://matrix-mcp.mindroom.chat/usage/) for arguments, examples, and numeric room and event refs.

## Development

```bash
uv sync --extra dev
uv run pytest
```

See [Contributing](https://matrix-mcp.mindroom.chat/contributing/) for linting, type checks, and releases.
