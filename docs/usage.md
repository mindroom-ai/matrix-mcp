---
icon: lucide/wrench
---

# Tool Reference

Matrix MCP exposes 30 tools.
Each one runs as the connected Matrix account, with that account's permissions, and declares whether it only reads or also writes.
Unless a section says otherwise, the same tools are available in local stdio mode and in [authenticated HTTP](hosted.md) mode.

| Area | Tools |
| --- | --- |
| [Session and rooms](#session-and-rooms) | `matrix_whoami`, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_set_room_name`, `matrix_set_room_topic`, `matrix_set_room_avatar` |
| [Reading](#reading) | `matrix_read_room_recent`, `matrix_read_thread`, `matrix_read_history`, `matrix_get_event_context` |
| [Writing](#writing) | `matrix_send_message`, `matrix_reply`, `matrix_react`, `matrix_edit_message`, `matrix_redact_event` |
| [Membership](#membership) | `matrix_list_room_members`, `matrix_search_users`, `matrix_invite_user`, `matrix_list_invitations`, `matrix_join_room`, `matrix_leave_room`, `matrix_create_room` |
| [Profiles](#profiles) | `matrix_get_profile`, `matrix_set_display_name`, `matrix_set_avatar` |
| [Media](#media) | `matrix_upload_media`, `matrix_download_media`, `matrix_send_media` |
| [Unread catch-up](#unread-catch-up) | `matrix_get_unread`, `matrix_mark_read` |

## Numeric Refs

In local mode, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_read_room_recent`, and `matrix_read_thread` return stable numeric refs next to the raw Matrix IDs.
Use them in later calls instead of copying long IDs:

```text
matrix_read_room_recent(room_id=1)
matrix_read_thread(room_id=1, thread_id=42)
matrix_send_message(room_id=1, body="reply", thread_id=42)
```

Room refs also work for the room member and room detail tools.
History, reply, reaction, edit, redaction, media, invitation, join and leave, and catch-up tools take raw Matrix IDs, in both modes.
Authenticated HTTP mode uses raw Matrix IDs everywhere.

Numeric refs are stored in a per-homeserver and per-user state file next to the credentials.
If a ref is unknown, list or read the relevant room or thread first.

## Safety

Matrix MCP is built so that an agent can read freely and writes only when asked:

- Read tools carry MCP read-only hints; tools that post, change, or remove something are marked as mutations, and the destructive ones (edits, redactions, leaving, room and profile setters) say so.
- The server instructs the agent to read first and to send messages, invite people, or change room and profile details only when the user explicitly asks.
- Reading never moves read markers.
  `matrix_mark_read` is a separate, explicit call that sends a private receipt by default.
- Uploads and downloads are capped at 5 MiB, and sync snapshots at 2 MiB of JSON.
- Edits and redactions are limited to the connected account's own events.
- Matrix MCP never asks for administrator, appservice, or provisioning credentials.

## Session and Rooms

### Identify the Session

```text
matrix_whoami()
```

Returns the configured Matrix user and device.

### List Rooms

```text
matrix_list_rooms()
```

Returns joined rooms.
Each room includes a stable numeric `id` ref and the raw Matrix `room_id`.

### Room Details

```text
matrix_get_room_info(room_id=1)
matrix_set_room_name(room_id=1, name="Project discussion")
matrix_set_room_topic(room_id=1, topic="Plans and updates")
matrix_set_room_avatar(room_id=1, avatar_url="mxc://example.com/room-avatar")
```

Read the current details before changing them.
Each setter changes one field and returns its Matrix event ID.
Pass an empty string to clear a name, topic, or avatar.
Room permissions apply normally; permission failures are returned as tool errors.
Avatars use existing `mxc://` media URIs; see [Media](#media) to upload one.

## Reading

### Recent Messages

```text
matrix_read_room_recent(room_id=1, limit=20)
```

`room_id` accepts either a numeric room ref returned by `matrix_list_rooms` or a full Matrix room ID.

Returned events include:

| Field | Meaning |
| --- | --- |
| `id` | Stable numeric event ref |
| `event_id` | Raw Matrix event ID |
| `thread_id` | Raw thread root event ID when the message is a thread reply |
| `thread_ref` | Numeric event ref for the thread root |
| `encrypted` | Whether the message was end-to-end encrypted |
| `decryption_error` | Why an encrypted message could not be read, if it could not |

### Threads

```text
matrix_read_thread(room_id=1, thread_id=42, limit=50)
```

`thread_id` accepts either a numeric event ref or a raw Matrix event ID.
The result holds the thread root and its newest replies, with edits applied.
`limit` counts every thread event, so non-message events such as polls can reduce the number of messages returned.

### History and Message Context

```text
matrix_read_history(room_id="!room:example.com", limit=20)
matrix_read_history(room_id="!room:example.com", limit=20, before="cursor-from-next_batch")
matrix_get_event_context(room_id="!room:example.com", event_id="$message", limit=10)
```

History pages contain newest-first events and a `next_batch` cursor for older messages.
Pass that cursor unchanged as `before`; `null` means the end.
History supports up to 100 entries per page; context supports up to 50 surrounding entries.
Results preserve message types, attachment metadata, reply and thread relationships, edits, and redacted placeholders.

??? info "How edits are resolved"

    Edit resolution accepts valid replacements from the original sender found in server bundles, the returned page or context, and an advertised bounded recovery scan.
    If a homeserver omits a replacement bundle and does not return the replacement alongside its original event, the original content may be shown.
    `edit_resolution_truncated` specifically reports an incomplete bounded recovery scan.

## Writing

!!! tip "Writes happen on request"

    The server tells agents to post, react, edit, or redact only when the user explicitly asks.

### Send Text or Files

```text
matrix_send_message(room_id=1, body="hello")
matrix_send_message(room_id=1, body="reply", thread_id=42)
matrix_send_message(room_id=1, body="Could you check this?", mentions=["@helper:example.com"])
matrix_send_message(room_id=1, file_path="workspace/report.txt")
matrix_send_message(room_id=1, file_path="workspace/report.txt", thread_id=42)
```

Pass `body` for text or `file_path` for a local file; `filename` and `content_type` optionally override what is sent for a file.
`mentions` takes full Matrix user IDs and adds explicit mentions, which is how you address a specific person or agent; it works for text messages only.
In encrypted rooms, text and files are both sent encrypted.
Local file sends are a stdio feature; authenticated HTTP mode sends text only and uses [Media](#media) for files.

### Replies, Reactions, and Corrections

```text
matrix_reply(room_id="!room:example.com", event_id="$message", body="I can help.")
matrix_react(room_id="!room:example.com", event_id="$message", key="👍")
matrix_edit_message(room_id="!room:example.com", event_id="$my-message", body="Corrected text")
matrix_redact_event(room_id="!room:example.com", event_id="$my-reaction")
```

Replies target a specific event and preserve its thread relationship.
Edits and redactions are limited to the connected account's own events.
Redacting your reaction removes it.

!!! warning "Redactions cannot be undone"

    Matrix redaction removes the event's content on the server; it is not a local delete you can reverse.

Event writes accept an optional `transaction_id`, so a client can retry the same intended operation without posting it twice.
Reuse a transaction ID only for an identical operation.

## Membership

### Members and Invitations

```text
matrix_list_room_members(room_id=1, limit=25)
matrix_list_room_members(room_id=1, limit=25, offset=25)
matrix_search_users(search_term="Bob", limit=10)
matrix_invite_user(room_id=1, user_id="@bob:example.com")
```

Member lists include joined users, their display names, and avatar URIs, sorted by user ID.
They exclude pending invitations and users who left.
Use the returned `next_offset` for another page; `null` means the end.
Each call reads current membership, so pages can change when people join or leave.
Member and directory queries accept limits from 1 to 100.

Directory visibility depends on the homeserver; `limited: true` means more matches exist, so narrow the search.
An invitation uses a full Matrix user ID and succeeds only when the connected account may invite that user.
It does not automatically join the invited user.

### Join, Leave, and Create Rooms

```text
matrix_list_invitations(limit=25)
matrix_join_room(room_id_or_alias="!invited:example.com")
matrix_join_room(room_id_or_alias="#project:example.com")
matrix_leave_room(room_id="!room:example.com", reason="No longer needed")
matrix_create_room(name="Project planning", invite=["@bob:example.com"])
```

Joining an invited room accepts its invitation; leaving an invited room declines it.
Invitation pages return `next_offset`; each page reflects a new filtered sync snapshot.
New rooms use the private-chat preset, are not published in the public directory, and do not enable encryption.
Other members still need to accept their invitations.

!!! note "Snapshot size"

    Invitation and catch-up offsets and limits are applied after the filtered sync snapshot is downloaded, so they do not reduce its upstream size.
    Snapshots over the 2 MiB JSON limit fail with a size error, whatever page limit you choose.

## Profiles

```text
matrix_get_profile()
matrix_get_profile(user_id="@bob:example.com")
matrix_set_display_name(displayname="Alice")
matrix_set_avatar(avatar_url="mxc://example.com/profile-avatar")
```

Profile setters change only the connected account's global profile, which may update its appearance across rooms.
Pass an empty string to clear a display name or avatar.
Avatar setters accept existing Matrix `mxc://` media URIs only, not HTTP URLs or local file paths.
Upload an image with `matrix_upload_media` or another Matrix client first, then use its media URI.

## Media

```text
matrix_upload_media(data_base64="SGVsbG8K", filename="hello.txt", content_type="text/plain")
matrix_send_media(room_id="!room:example.com", media_url="mxc://example.com/uploaded", filename="hello.txt", content_type="text/plain", size=6)
matrix_download_media(media_url="mxc://example.com/uploaded")
matrix_download_media(media_url="mxc://example.com/sealed", room_id="!room:example.com", event_id="$file")
```

Uploads return `content_uri`, filename, MIME type, and decoded byte size.
Pass `content_type` as a bare MIME type/subtype, such as `image/png`, without parameters.
Use the returned URI to send a file, or pass an uploaded image as `avatar_url` to an avatar setter.
`matrix_send_media` accepts an optional `thread_id` and `transaction_id`.
Downloads return base64 content and metadata; pass the message's `room_id` and `event_id` as well to download an encrypted attachment.

Each upload or download is limited to 5 MiB of decoded data.
Downloads use the homeserver's authenticated media API and require its support for that endpoint.
HTTP URLs, redirects, and server filesystem paths are not accepted.
Transfers request identity HTTP encoding; servers that force HTTP compression are rejected to preserve the byte limit.

!!! warning "Uploaded media is not encrypted"

    `matrix_upload_media` stores files unencrypted, and `matrix_send_media` refuses encrypted rooms.
    To share a file in an encrypted room, use `matrix_send_message` with `file_path`.
    See [Files](encryption.md#files).

## Unread Catch-Up

```text
matrix_get_unread(limit=25, timeline_limit=20)
matrix_get_unread(limit=25, offset=25, timeline_limit=20)
matrix_mark_read(room_id="!room:example.com", event_id="$last-read")
```

Catch-up returns rooms selected by homeserver notification and highlight counts or a marked-unread flag.
Explicit mentions are bounded details within those rooms and do not independently make an already-read room unread.
Counts depend on homeserver push rules; recent mention events are not a complete historical search.
Room results include the server's timeline truncation flag and older-history cursor.
Follow `next_offset` for more rooms.
Each call fetches a fresh snapshot, so concurrent activity may change pages.
No background sync or unread state is stored by the MCP server.

`matrix_mark_read` updates the fully-read marker, clears the room's marked-unread flag, and sends a private receipt by default.
Pass `public_receipt=true` only when the user wants others to see the receipt.
History and catch-up reads never mark messages read automatically.
