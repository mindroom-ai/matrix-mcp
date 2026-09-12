---
icon: lucide/terminal
---

# Usage

## Authentication Commands

### SSO Login

```bash
matrix-mcp auth sso https://mindroom.chat
```

### SSO Provider Discovery

```bash
matrix-mcp auth providers https://mindroom.chat
```

### SSO on a Remote or Headless Machine

`auth sso` waits for the browser to hit a callback server on the machine running the command.
When that machine has no browser (for example over SSH), either pin the callback port and forward it with `ssh -L`, or exchange the login token manually:

```bash
matrix-mcp auth sso https://mindroom.chat --callback-port 8765   # with ssh -N -L 8765:127.0.0.1:8765
matrix-mcp auth sso-url https://mindroom.chat http://127.0.0.1:8765/callback
matrix-mcp auth login-token https://mindroom.chat syt_...
```

See the [getting started guide](getting-started.md#sso-over-ssh-or-on-a-headless-machine) for the full walkthrough.

### Access Gateways

Some homeservers sit behind an access gateway that requires extra HTTP headers.
Static headers can be stored during login:

```bash
matrix-mcp auth sso https://mindroom.chat \
  --header "X-Access-Client-Id: ..." \
  --header "X-Access-Client-Secret: ..."
```

For short-lived headers, store a command that prints the current value.
Matrix MCP reruns this command when creating a Matrix client for later MCP tool calls:

```bash
matrix-mcp auth sso https://mindroom.chat \
  --header-command "X-Access-Token: access-gateway-cli token --app https://mindroom.chat"
```

For Cloudflare Access, use the built-in preset instead.
It stores a dynamic `cf-access-token` header command backed by the local `cloudflared` CLI.
During setup, it runs `cloudflared access login` first if no token is available.
On macOS with Homebrew, install it first:

```bash
brew install cloudflared
matrix-mcp auth sso https://mindroom.chat --cloudflare-access
```

For other platforms, install `cloudflared` from Cloudflare's downloads page.

### Logout

```bash
matrix-mcp auth logout
```

## MCP Tools

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

### Read Recent Room Messages

```text
matrix_read_room_recent(room_id=1, limit=20)
```

`room_id` accepts either:

- a numeric room ref returned by `matrix_list_rooms`
- a full Matrix room ID

Returned events include:

- `id`: stable numeric event ref
- `event_id`: raw Matrix event ID
- `thread_id`: raw thread root event ID when the message is a thread reply
- `thread_ref`: numeric event ref for the thread root

### Read a Thread

```text
matrix_read_thread(room_id=1, thread_id=42, limit=50)
```

`thread_id` accepts either a numeric event ref or a raw Matrix event ID.

### Send Text or Files

```text
matrix_send_message(room_id=1, body="hello")
matrix_send_message(room_id=1, body="reply", thread_id=42)
matrix_send_message(room_id=1, file_path="workspace/report.txt")
matrix_send_message(room_id=1, file_path="workspace/report.txt", thread_id=42)
```

Use send tools only when the user explicitly asks to post.

### Room Members and Invitations

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

### Room Details

```text
matrix_get_room_info(room_id=1)
matrix_set_room_name(room_id=1, name="Project discussion")
matrix_set_room_topic(room_id=1, topic="Plans and updates")
matrix_set_room_avatar(room_id=1, avatar_url="mxc://example.com/room-avatar")
```

Read the current details before changing them.
Each setter changes one field and returns its Matrix event ID.
Room permissions apply normally; permission failures are returned as tool errors.

### User Profiles

```text
matrix_get_profile()
matrix_get_profile(user_id="@bob:example.com")
matrix_set_display_name(displayname="Alice")
matrix_set_avatar(avatar_url="mxc://example.com/profile-avatar")
```

Profile setters change only the connected account's global profile, which may update its appearance across rooms.
Avatar setters use existing Matrix `mxc://` media URIs.
Upload an image with `matrix_upload_media` or another Matrix client first, then use its media URI; HTTP URLs and local file paths are not accepted by avatar setters.
Pass an empty string to clear a display name, room name, topic, or avatar.
Invite people and change room/profile details only when the user explicitly requests that action.

These tools also work in authenticated HTTP mode, using raw Matrix room IDs instead of numeric references.

### History and Message Context

The following tools use raw Matrix IDs in both stdio and HTTP mode.

```text
matrix_read_history(room_id="!room:example.com", limit=20)
matrix_read_history(room_id="!room:example.com", limit=20, before="cursor-from-next_batch")
matrix_get_event_context(room_id="!room:example.com", event_id="$message", limit=10)
```

History pages contain newest-first events and a `next_batch` cursor for older messages.
Pass that cursor unchanged as `before`; `null` means the end.
History supports up to 100 entries per page; context supports up to 50 surrounding entries.
Results preserve message types, attachment metadata, reply and thread relationships, edits, and redacted placeholders.
Edit resolution accepts only valid replacements from the original sender and reports when its bounded scan is incomplete.
Reading never changes read markers.

### Replies, Reactions, and Corrections

```text
matrix_reply(room_id="!room:example.com", event_id="$message", body="I can help.")
matrix_react(room_id="!room:example.com", event_id="$message", key="👍")
matrix_edit_message(room_id="!room:example.com", event_id="$my-message", body="Corrected text")
matrix_redact_event(room_id="!room:example.com", event_id="$my-reaction")
```

Replies target a specific event and preserve its thread relationship.
Edits and redactions are limited to the connected account's own events.
Redacting your reaction removes it; Matrix redaction removes content and is not an undoable local delete.
Event writes accept an optional `transaction_id` so clients can retry the same intended operation without posting it twice.
Reuse a transaction ID only for an identical operation.
Use these actions only when explicitly requested.

### Join, Leave, and Create Rooms

```text
matrix_list_invitations(limit=25)
matrix_join_room(room_id_or_alias="!invited:example.com")
matrix_join_room(room_id_or_alias="#project:example.com")
matrix_leave_room(room_id="!room:example.com", reason="No longer needed")
matrix_create_room(name="Project planning", invite=["@bob:example.com"])
```

Joining an invited room accepts its invitation; leaving an invited room declines it.
Invitation pages return `next_offset`; each page reflects current server state.
New rooms use the private-chat preset, are not published in the public directory, and do not enable encryption.
Other members still need to accept their invitations.

### Files, Images, and Avatars

```text
matrix_upload_media(data_base64="SGVsbG8K", filename="hello.txt", content_type="text/plain")
matrix_send_media(room_id="!room:example.com", media_url="mxc://example.com/uploaded", filename="hello.txt", content_type="text/plain", size=6)
matrix_download_media(media_url="mxc://example.com/uploaded")
```

Uploads return `content_uri`, filename, MIME type, and decoded byte size.
Pass `content_type` as a bare MIME type/subtype, such as `image/png`, without parameters.
Use that URI to send a file or, after uploading an image, pass it as `avatar_url` to an existing avatar setter.
Downloads return base64 content and metadata.
Each upload or download is limited to 5 MiB of decoded data.
Media downloads use the configured homeserver's authenticated media API and require its support for that endpoint.
HTTP URLs, redirects, and server filesystem paths are not accepted.
Transfers request identity HTTP encoding; servers that force HTTP compression are rejected to preserve the byte limit.
`matrix_send_media` accepts an optional `thread_id` and `transaction_id`.
File and image sends perform the same best-effort encryption check as new text actions.

### Unread Catch-Up

```text
matrix_get_unread(limit=25, timeline_limit=20)
matrix_get_unread(limit=25, offset=25, timeline_limit=20)
matrix_mark_read(room_id="!room:example.com", event_id="$last-read")
```

Catch-up returns rooms with unread or mention information, server notification/highlight counts, and mentions found in each room's bounded recent timeline.
Counts depend on homeserver push rules; recent mention events are not a complete historical search.
Room results include the server's timeline truncation flag and older-history cursor.
Follow `next_offset` for more rooms.
Each call fetches a fresh snapshot, so concurrent activity may change pages.
No background sync or unread state is stored by the MCP server.
`matrix_mark_read` updates the fully-read marker and sends a private receipt by default.
Pass `public_receipt=true` only when the user wants others to see the receipt.
History and catch-up reads never mark messages read automatically.

## Stored Files

Credentials are stored in the user config directory reported by:

```bash
matrix-mcp config-path
```

Numeric refs are stored in a separate per-homeserver/user state file in the same config directory.
If a numeric ref is unknown, read or list the relevant room/thread first.
