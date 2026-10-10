---
icon: lucide/wrench
---

# Tool Reference

Matrix MCP exposes 43 tools.
Each one runs as the connected Matrix account, with that account's permissions, and declares whether it only reads or also writes.
Unless a section says otherwise, the same tools are available in local stdio mode and in [authenticated HTTP](hosted.md) mode.

| Area | Tools |
| --- | --- |
| [Session and rooms](#session-and-rooms) | `matrix_whoami`, `matrix_list_rooms`, `matrix_get_room_info`, `matrix_set_room_name`, `matrix_set_room_topic`, `matrix_set_room_avatar`, `matrix_get_space_hierarchy` |
| [Reading](#reading) | `matrix_read_room_recent`, `matrix_read_thread`, `matrix_list_threads`, `matrix_read_history`, `matrix_get_event_context`, `matrix_search_messages`, `matrix_get_reactions`, `matrix_get_read_receipts` |
| [Writing](#writing) | `matrix_send_message`, `matrix_reply`, `matrix_react`, `matrix_edit_message`, `matrix_redact_event`, `matrix_pin_message`, `matrix_unpin_message` |
| [Membership](#membership) | `matrix_list_room_members`, `matrix_search_users`, `matrix_invite_user`, `matrix_list_invitations`, `matrix_join_room`, `matrix_leave_room`, `matrix_create_room`, `matrix_create_dm` |
| [Moderation](#moderation) | `matrix_get_power_levels`, `matrix_set_power_level`, `matrix_kick_user`, `matrix_ban_user`, `matrix_unban_user` |
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

- Read tools carry MCP read-only hints; tools that post, change, or remove something are marked as mutations, and the destructive ones (edits, redactions, leaving, kicks, bans, power levels, room and profile setters) say so.
- The server instructs the agent to read first and to send messages, invite people, pin messages, moderate members, or change room and profile details only when the user explicitly asks.
- It refuses to kick or ban the connected account, to lower its own power level, or to change a room creator's level.
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
matrix_list_rooms(sort="activity")
matrix_list_rooms(sort="name")
```

Returns joined rooms.
Each room includes a stable numeric `id` ref and the raw Matrix `room_id`.
`sort="activity"` puts the rooms with the newest messages first and fills in `last_activity_ms`; `sort="name"` sorts A to Z.
Activity looks at each room's ten newest events, so a room whose recent events are all state changes, such as membership updates, may show no activity on some homeservers.

### Room Details

```text
matrix_get_room_info(room_id=1)
matrix_set_room_name(room_id=1, name="Project discussion")
matrix_set_room_topic(room_id=1, topic="Plans and updates")
matrix_set_room_avatar(room_id=1, avatar_url="mxc://example.com/room-avatar")
```

`matrix_get_room_info` also reports whether the room is `encrypted`, its `joined_member_count`, the connected user's `own_power_level`, its `room_type` (`m.space` for spaces), and its `pinned_event_ids`.
These extra details are best effort: for a room the connected user has left, they can be `null` while the name and topic still show.
In room version 12 and later, room creators outrank every power level, so a creator's `own_power_level` is `null`.

Read the current details before changing them.
Each setter changes one field and returns its Matrix event ID.
Pass an empty string to clear a name, topic, or avatar.
Room permissions apply normally; permission failures are returned as tool errors.
Avatars use existing `mxc://` media URIs; see [Media](#media) to upload one.

### Spaces

```text
matrix_get_space_hierarchy(space_id="!space:example.com")
matrix_get_space_hierarchy(space_id="!space:example.com", max_depth=2, next_batch="cursor")
```

Lists the space itself and the rooms and subspaces inside it, up to `max_depth` levels deep (1 to 5).
Each entry has its name, topic, alias, `room_type`, `joined_member_count`, `join_rule`, its `children`, and whether the connected user has `joined` it.
Rooms that another server describes invalidly are left out and counted in `skipped`.
Join a listed room with `matrix_join_room`.

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

### List Threads

```text
matrix_list_threads(room_id="!room:example.com")
matrix_list_threads(room_id="!room:example.com", include="participated", before="cursor-from-next_batch")
```

Lists a room's threads, newest first, up to 50 per page.
Each thread has its `root` message, `reply_count`, `latest_reply`, and whether the connected user `participated`, with edits resolved as in [history](#history-and-message-context).
`include="participated"` keeps only threads the connected user has posted in.
Pass `next_batch` unchanged as `before` for older threads, then read one with `matrix_read_thread`.

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

### Search

```text
matrix_search_messages(search_term="release date")
matrix_search_messages(search_term="release date", room_id="!room:example.com", order_by="rank", limit=20)
```

Uses the homeserver's full-text search across joined rooms, or within one room.
Results are newest first by default; `order_by="rank"` puts the best matches first.
Each result has the `room_id`, the matching `event`, and the server's `rank`.
When the match is an edit, `edit_of` names the original message and `body` shows the edited text.
Pass `next_batch` unchanged for more results, and open a match with `matrix_get_event_context` to see the discussion around it.

!!! note "Encrypted rooms are not searchable"

    The homeserver cannot read end-to-end encrypted messages, so it cannot index them.
    Search finds only messages in unencrypted rooms; page through encrypted rooms with `matrix_read_history` instead.
    How matches are found (word stemming, partial words) depends on the homeserver.

### Reactions

```text
matrix_get_reactions(room_id="!room:example.com", event_id="$proposal")
```

Summarizes the reactions on one event: each key, how many users used it, and who.
Reactions in encrypted rooms are counted too.
It scans up to `limit` reaction events (200 by default, at most 500); `truncated` reports when there were more.

### Read Receipts

```text
matrix_get_read_receipts(room_id="!room:example.com")
matrix_get_read_receipts(room_id="!room:example.com", event_id="$my-message")
```

Shows each member's latest read receipt, newest first: the event they have read up to, when, and in which thread.
Pass `event_id` to check who has read that message: `read` is `true` when a member's receipt is on that event or a later one, by server timestamps, and `null` when it could not be checked, including receipts from a different thread than the message.
Members who send private receipts or have receipts turned off do not appear; the connected user's own private receipt is marked `private`, though some homeservers (Tuwunel) do not report a private receipt on the user's own message.

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

### Pins

```text
matrix_pin_message(room_id="!room:example.com", event_id="$runbook")
matrix_unpin_message(room_id="!room:example.com", event_id="$runbook")
```

Pinning adds a message to the room's pinned list, which chat apps show at the top of the room; unpinning removes it.
Both return the updated `pinned` list, and `changed: false` when nothing needed to change.
Changing pins requires the room permission to send `m.room.pinned_events`.

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

### Direct Chats

```text
matrix_create_dm(user_id="@bob:example.com")
matrix_create_dm(user_id="@bob:example.com", encrypted=true)
```

Opens a direct chat with one user and marks it as direct, so chat apps list it under people.
An existing direct chat is reused, with `created: false`, only when it holds exactly the two of you (joined or invited) and suits the request: `encrypted=true` never reuses an unencrypted chat, and authenticated HTTP mode never reuses an encrypted one.
`encrypted=true` creates an end-to-end encrypted chat; it needs local mode, because [authenticated HTTP](hosted.md) mode holds no encryption keys.
Messages sent before the other user accepts the invitation are shared with their devices, so they can read them after joining.

!!! note "Snapshot size"

    Invitation and catch-up offsets and limits are applied after the filtered sync snapshot is downloaded, so they do not reduce its upstream size.
    Snapshots over the 2 MiB JSON limit fail with a size error, whatever page limit you choose.

## Moderation

```text
matrix_get_power_levels(room_id="!room:example.com")
matrix_set_power_level(room_id="!room:example.com", user_id="@bob:example.com", level=50)
matrix_set_power_level(room_id="!room:example.com", user_id="@bob:example.com", level=null)
matrix_kick_user(room_id="!room:example.com", user_id="@spam:example.com", reason="Spam")
matrix_ban_user(room_id="!room:example.com", user_id="@spam:example.com", reason="Spam")
matrix_unban_user(room_id="!room:example.com", user_id="@spam:example.com")
```

`matrix_get_power_levels` shows who can do what: each user's level, the default level, the levels needed to invite, kick, ban, redact, and send each event type, the room `creators`, and the connected user's `own_level`.
`matrix_set_power_level` changes one user's level and leaves the rest of the power levels untouched; `level=null` resets the user to the room default.
A kick removes a user, who may rejoin if the room allows it; a ban keeps them out until `matrix_unban_user` lifts it.
Unbanning does not invite the user back.

All of these use the connected account's own permissions, so the homeserver rejects anything it may not do.
The tools additionally refuse to kick or ban the connected account (use `matrix_leave_room`), to lower its own power level, and to change a room creator's level, since none of those can be undone by the account itself.

!!! warning "Moderation is destructive"

    Kicks, bans, and power level changes take effect for everyone in the room immediately.
    MCP clients see these tools marked as destructive, and the server tells agents to use them only when the user explicitly asks.

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
