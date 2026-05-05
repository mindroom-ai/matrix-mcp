---
icon: lucide/terminal
---

# Usage

## Authentication Commands

### SSO Login

```bash
matrix-mcp auth sso https://matrix.example.com
```

### SSO Provider Discovery

```bash
matrix-mcp auth providers https://matrix.example.com
```

### Access Gateways

Some homeservers sit behind an access gateway that requires extra HTTP headers.
Static headers can be stored during login:

```bash
matrix-mcp auth sso https://matrix.example.com \
  --header "X-Access-Client-Id: ..." \
  --header "X-Access-Client-Secret: ..."
```

For short-lived headers, store a command that prints the current value.
Matrix MCP reruns this command when creating a Matrix client for later MCP tool calls:

```bash
matrix-mcp auth sso https://matrix.example.com \
  --header-command "X-Access-Token: access-gateway-cli token --app https://matrix.example.com"
```

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

## Stored Files

Credentials are stored in the user config directory reported by:

```bash
matrix-mcp config-path
```

Numeric refs are stored in a separate per-homeserver/user state file in the same config directory.
If a numeric ref is unknown, read or list the relevant room/thread first.
