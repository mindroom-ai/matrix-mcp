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
matrix-mcp auth sso https://mindroom.chat
claude mcp add matrix -- matrix-mcp serve
```

For Codex, use:

```bash
codex mcp add matrix -- matrix-mcp serve
```

Continue with [Getting Started](getting-started.md), or see the [usage guide](usage.md) for the full tool surface.

## Features

- Matrix SSO, password, login-token, or existing access-token setup, including access gateways that need extra headers.
- Read rooms, threads, history, and unread mentions; reply, react, edit, and send files.
- Manage room membership, room details, and your profile.
- [End-to-end encryption](encryption.md) in local mode: encrypted rooms work like any other room.
- [Authenticated HTTP](hosted.md) for remote clients, where each caller connects their own Matrix account.

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

See the [usage guide](usage.md) for arguments and examples.

## Numeric Refs

In local mode, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_read_room_recent`, and `matrix_read_thread` also return stable numeric refs.
Use them in later calls instead of copying raw Matrix IDs:

```text
matrix_read_room_recent(room_id=1)
matrix_read_thread(room_id=1, thread_id=42)
matrix_send_message(room_id=1, body="reply", thread_id=42)
```

Room refs also work for the room member and room detail tools.
History, reply, reaction, edit, redaction, media, invitation, join and leave, and catch-up tools take raw Matrix IDs.
