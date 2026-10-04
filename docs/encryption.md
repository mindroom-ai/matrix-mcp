---
icon: lucide/lock
---

# End-to-End Encryption

In stdio mode, Matrix MCP is its own Matrix device with end-to-end encryption.
Encrypted rooms work with the same tools as any other room: reads are decrypted and sends are encrypted, including files.
Authenticated HTTP mode does not support encrypted rooms.

## Setup

Every `matrix-mcp auth` login publishes the device's encryption keys and prints its fingerprint; `auth token` does so only when given `--device-id`.
If that step fails, unencrypted rooms still work; retry it with:

```bash
matrix-mcp e2ee setup
```

SSO, password, and login-token logins create a new device for Matrix MCP.
`auth token` reuses the device the access token belongs to, which works with encryption only if no other client has published keys for that device.

## Reading Older Messages

Other clients share a room's keys only with devices that exist when they send.
Messages sent before your login come back with a null `body` and `decryption_error: "missing room key"`.
To read them, export your room keys from another Matrix client to a passphrase-protected file and import it:

```bash
matrix-mcp e2ee import-keys element-keys.txt
```

## Files

Attachments in encrypted rooms show `media.encrypted: true`.
To download one, pass its `media_url` together with the `room_id` and `event_id` of its message to `matrix_download_media`.

To share a file in an encrypted room, use `matrix_send_message` with `file_path`; it uploads an encrypted copy.
`matrix_upload_media` stores files unencrypted, and `matrix_send_media` refuses encrypted rooms.

## Where the Keys Live

Keys are stored per device in the default config directory reported by `matrix-mcp config-path`, even when you use `--config`, and only your user can read them.
Several `matrix-mcp serve` processes on one machine can share the device; they take turns using the keys.
Log in separately on each machine instead of copying the config directory: two machines sharing one device would split its room keys between them.

`matrix-mcp auth logout` deletes the keys but leaves the device registered on the homeserver.
Remove it from your session list in another client if you no longer need it.

## Limitations

- The device is not verified, so other clients list it as an unverified session. Clients set to withhold keys from unverified devices will not share room keys with it.
- Matrix MCP does not verify other devices either, so it trusts the homeserver to name the sender of each message.
- Room keys go to joined members. Invited users cannot read messages sent before they join.
- A message is not sent when its room key fails to reach a device that should get it. Devices that cannot be reached at all, such as devices out of one-time keys or on an unreachable server, are skipped, as in other clients.
- Edits of encrypted messages count only when the edit is encrypted too.
- The encryption check and the send are separate requests, so a room that turns on encryption between them can receive one plaintext message.
- Replayed encrypted messages are detected only within a single tool call.
- Repairing a broken encryption session with another device is best effort.
