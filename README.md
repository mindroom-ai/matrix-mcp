<div align="center">

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/matrix-mcp/main/docs/assets/logo.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/matrix-mcp/main/docs/assets/logo-static.svg" alt="Matrix MCP logo" width="128" />
</picture>

# Matrix MCP

**Give your AI agent a seat in every Matrix room.**

A full-featured [MCP](https://modelcontextprotocol.io/) server for [Matrix](https://matrix.org/).
Claude Code, Codex, and any other MCP client can catch up on your rooms, read threads and history, reply, share files, and manage rooms, including end-to-end encrypted ones.

[![PyPI](https://img.shields.io/pypi/v/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![Python Versions](https://img.shields.io/pypi/pyversions/matrix-mcp.svg)](https://pypi.org/project/matrix-mcp/)
[![CI](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mindroom-ai/matrix-mcp/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-matrix--mcp.mindroom.chat-0b6f86)](https://matrix-mcp.mindroom.chat/)
[![MCP](https://img.shields.io/badge/MCP-server-0b6f86)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/mindroom-ai/matrix-mcp/blob/main/LICENSE)

**[Documentation](https://matrix-mcp.mindroom.chat/)** ·
[Getting Started](https://matrix-mcp.mindroom.chat/getting-started/) ·
[Tool Reference](https://matrix-mcp.mindroom.chat/usage/) ·
[Encryption](https://matrix-mcp.mindroom.chat/encryption/) ·
[Hosted Mode](https://matrix-mcp.mindroom.chat/hosted/)

</div>

## Quick Start

```bash
uv tool install matrix-mcp
matrix-mcp auth sso https://mindroom.chat   # your homeserver; opens a browser for SSO

claude mcp add matrix -- matrix-mcp serve   # Claude Code
codex mcp add matrix -- matrix-mcp serve    # Codex
```

Then ask your agent *"What did I miss in the release room?"*

The server talks to your MCP client over stdio and opens no local port.
[Getting Started](https://matrix-mcp.mindroom.chat/getting-started/) covers SSO providers, logging in over SSH or on a headless machine, password and access-token login, and homeservers behind access gateways such as Cloudflare Access.

## What You Can Ask

| Ask your agent | Tools it reaches for |
| --- | --- |
| *"Catch me up: which rooms need me, and what was I mentioned in?"* | `matrix_get_unread`, `matrix_read_thread` |
| *"Read the deploy thread and reply with the fix I just pushed."* | `matrix_read_thread`, `matrix_send_message` |
| *"Post `coverage.html` to the CI room and ask the reviewer to take a look."* | `matrix_send_message` |
| *"Download the screenshot Alice posted and tell me what's broken."* | `matrix_read_room_recent`, `matrix_download_media` |
| *"Spin up a room for this incident and invite Bob and Carol."* | `matrix_create_room`, `matrix_search_users` |
| *"Find where we picked the release date and show me the discussion around it."* | `matrix_read_history`, `matrix_get_event_context` |

## Highlights

- 🧰 **30 tools.** Reading, threads, history, unread catch-up, replies, reactions, edits, redactions, invitations, rooms, profiles, and media.
- 🔒 **End-to-end encryption.** Matrix MCP is its own Matrix device: encrypted rooms just work, files included, and older keys import from Element.
- 🔑 **Every login your server allows.** Matrix SSO (even over SSH), password, login token, or access token, including homeservers behind Cloudflare Access and other gateways.
- 🌐 **Local or hosted.** stdio for local agents, or [authenticated HTTP](https://matrix-mcp.mindroom.chat/hosted/) where every user signs in with their own Matrix account through OAuth and Matrix SSO. Container images on GHCR.
- #️⃣ **Light on context.** Rooms and events get short numeric refs, so agents write `room_id=3` instead of copying long Matrix IDs.
- 🛡️ **Careful by design.** Every tool declares whether it writes, reading never marks messages read, receipts are private by default, and transfers are size-bounded.

As far as we know, Matrix MCP is the only Matrix MCP server that runs both as a local server with end-to-end encryption and as a multi-user hosted service where everyone signs in with their own Matrix account.

## Tools

| Area | Tools |
| --- | --- |
| Session and rooms | `matrix_whoami`, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_set_room_name`, `matrix_set_room_topic`, `matrix_set_room_avatar` |
| Reading | `matrix_read_room_recent`, `matrix_read_thread`, `matrix_read_history`, `matrix_get_event_context` |
| Writing | `matrix_send_message`, `matrix_reply`, `matrix_react`, `matrix_edit_message`, `matrix_redact_event` |
| Membership | `matrix_list_room_members`, `matrix_search_users`, `matrix_invite_user`, `matrix_list_invitations`, `matrix_join_room`, `matrix_leave_room`, `matrix_create_room` |
| Profile | `matrix_get_profile`, `matrix_set_display_name`, `matrix_set_avatar` |
| Media | `matrix_upload_media`, `matrix_download_media`, `matrix_send_media` |
| Catch-up | `matrix_get_unread`, `matrix_mark_read` |

All actions use the connected account's Matrix permissions.
The [Tool Reference](https://matrix-mcp.mindroom.chat/usage/) has arguments, examples, and limits for each tool.

## Development

```bash
uv sync --extra dev
uv run pytest
```

See [Contributing](https://matrix-mcp.mindroom.chat/contributing/) for linting, type checks, docs, and releases.

## Built by MindRoom

<a href="https://mindroom.chat"><img src="https://raw.githubusercontent.com/mindroom-ai/matrix-mcp/main/docs/assets/mindroom.png" alt="MindRoom logo" width="64" align="left" /></a>

Matrix MCP is built by [MindRoom](https://mindroom.chat), open-source AI agents that live in Matrix.
We use it to bring our local coding agents into the same rooms as our teammates and our MindRoom agents.
It works with any Matrix homeserver; no MindRoom account needed.

<br clear="left" />

MIT licensed.
