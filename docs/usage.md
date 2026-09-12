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
Upload an image through a Matrix client first, then use its media URI; HTTP URLs and local file paths are not accepted by avatar setters.
Pass an empty string to clear a display name, room name, topic, or avatar.
Invite people and change room/profile details only when the user explicitly requests that action.

These tools also work in authenticated HTTP mode, using raw Matrix room IDs instead of numeric references.

## Stored Files

Credentials are stored in the user config directory reported by:

```bash
matrix-mcp config-path
```

Numeric refs are stored in a separate per-homeserver/user state file in the same config directory.
If a numeric ref is unknown, read or list the relevant room/thread first.
