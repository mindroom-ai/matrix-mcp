# Authenticated HTTP

`matrix-mcp serve` uses local stdio by default.
HTTP mode lets each MCP client connect its own Matrix account through the configured homeserver's browser SSO.
It uses FastMCP's OAuth proxy, client registration, consent, PKCE, and encrypted persistent storage.
No shared Matrix access token or local auth config is used.

## Setup

The homeserver must support `m.login.sso` and `m.login.token`.
Configure one homeserver and one public MCP origin.
Put HTTPS in front of the HTTP listener.
Public URLs require HTTPS; loopback HTTP is allowed for local development.

Generate a stable secret once and store it in your deployment's secret manager:

```sh
openssl rand -hex 32
```

Provide that value through `MATRIX_MCP_HOSTED_SECRET_KEY`.
Use at least 32 random characters.
Do not regenerate it at each startup or pass it on the command line.
Losing the key loses access to stored registrations and sessions.
The server refuses to open existing state with a different key or homeserver.

```sh
matrix-mcp serve --transport http \
  --host 127.0.0.1 --port 8000 \
  --public-base-url https://mcp.example.com \
  --homeserver https://matrix.example.com \
  --state-directory ./matrix-mcp-state \
  --allowed-client-redirect-uri https://client.example.com/oauth/callback
```

Register the exact callback URI used by each MCP client.
Repeat the callback option for multiple clients.
Wildcards are rejected.
A loopback client callback such as `http://127.0.0.1:8765/callback` is allowed when explicitly configured.
Dynamic registration accepts public clients using PKCE (`token_endpoint_auth_method` set to `none`), with the `authorization_code` and `refresh_token` grants and the single `matrix` scope.
Client metadata document discovery is disabled.

Every setting also supports a `MATRIX_MCP_HOSTED_` environment variable:

| Variable suffix | Meaning |
| --- | --- |
| `PUBLIC_BASE_URL` | Public MCP origin, such as `https://mcp.example.com` |
| `HOMESERVER` | Public Matrix homeserver used for browser SSO |
| `API_BASE_URL` | Optional trusted server-side Matrix API base; HTTP allowed |
| `STATE_DIRECTORY` | Persistent directory for encrypted OAuth state |
| `SECRET_KEY` | Required stable signing and encryption secret |
| `ALLOWED_CLIENT_REDIRECT_URIS` | JSON array of exact allowed callback URIs |

For example, `MATRIX_MCP_HOSTED_ALLOWED_CLIENT_REDIRECT_URIS` may contain `["https://client.example.com/oauth/callback"]`.
CLI values override environment values.
Request input cannot choose the homeserver or API base.

An HTTP `API_BASE_URL` carries Matrix bearer tokens without transport encryption.
Use it only on a protected internal network; use HTTPS otherwise.

Keep the state directory on a persistent local volume, accessible only to the service account.
Run **one process per state directory**.
Startup holds a file lock for the application lifetime; a second server fails to start.
Multiple workers, replicas, and network filesystems are unsupported.
Back up encrypted state and the secret securely together.
Stop the server before restoring state.

## Container

Release CI publishes Linux AMD64 and ARM64 images to `ghcr.io/mindroom-ai/matrix-mcp`, tagged with the release tag (such as `v0.6.1`).
`latest` follows stable releases.
The image contains the same wheel published to PyPI, with dependencies pinned by `uv.lock`.
Pin a version or image digest when you need predictable upgrades.

With the stable secret exported as described above:

```sh
docker run --rm --read-only --tmpfs /tmp \
  --publish 127.0.0.1:8000:8000 \
  --mount type=volume,src=matrix-mcp-state,dst=/data \
  --env MATRIX_MCP_HOSTED_SECRET_KEY \
  ghcr.io/mindroom-ai/matrix-mcp:latest serve --transport http \
  --host 0.0.0.0 --public-base-url https://mcp.example.com \
  --homeserver https://matrix.example.com --state-directory /data/oauth \
  --allowed-client-redirect-uri https://client.example.com/oauth/callback
```

Put your HTTPS proxy in front of port 8000.
The image runs as UID/GID `10001`; bind-mounted state directories must be writable by that identity.
Keep `/data` and the signing key across replacements.
One container serves multiple users; each request uses its caller's Matrix credentials.
The single-process storage limits above still apply.

To build from a checkout:

```sh
uv build --wheel
uv export --locked --no-dev --no-emit-project --no-hashes --output-file dist/requirements.txt
docker build -t matrix-mcp:local .
```

PR CI builds and smoke-tests both architectures without publishing.
Release publishing uses the repository's `GITHUB_TOKEN`; no separate registry credential is needed.
On first publication, a package administrator must make the GHCR package public for anonymous pulls.

## Connect a client

Use `https://mcp.example.com/mcp` as the remote MCP URL.
Authorization and protected-resource discovery advertise the OAuth endpoints and `matrix` scope.
The client registers its callback, opens the consent screen, then redirects to Matrix SSO.
The browser returns to `https://mcp.example.com/auth/callback` with a single-use Matrix `loginToken`; that token is exchanged only by the server.

The CLI disables HTTP access logs because callback queries contain login tokens.
Configure reverse proxies and observability systems to omit callback query strings and authorization headers.
Keep auth debug logging disabled.
Deploy public registration and login endpoints behind appropriate request limits.
OAuth request bodies are limited to 64 KiB and must arrive within ten seconds.
Client body reception and response delivery occur outside the state mutation lock; discovery remains available while authorization state changes are in progress.
Upstream Matrix calls remain inside serialized OAuth mutations, so a slow upstream queues other OAuth mutations.
Discovery and MCP request authentication stay outside this lock.

## Sessions and revocation

MCP clients receive server-issued tokens, never Matrix access or refresh tokens.
MCP access tokens last at most one hour, bounded by Matrix's advertised lifetime.
Missing Matrix expiry uses the one-hour legacy default; malformed advertised expiry is rejected.
MCP refresh tokens rotate on every use and expire after 30 days without renewal.
Real Matrix refresh tokens are renewed upstream.
For legacy Matrix sessions without refresh tokens, an encrypted private credential retains that session and validates it with Matrix `whoami` before issuing fresh MCP tokens.

Every authenticated MCP request checks the current Matrix access token through `whoami`.
The returned user and device identify the tool caller.
Matrix room permissions apply normally.
Remote token invalidation causes requests to fail; access checks never silently refresh an invalidated Matrix session.

MCP token revocation invalidates the entire local token lineage, including access tokens from earlier refreshes, before attempting Matrix device logout.
If Matrix logout fails, local access stays revoked.
Another MCP client's token cannot revoke your connection.
Use a current refresh token to revoke a connection whose access token has expired or whose Matrix session is already invalid.
Signing out of the upstream identity provider does **not** itself revoke an existing Matrix session.
Revoke the Matrix device/session or the MCP connection.

## Tools and limits

Hosted mode exposes the [conversation, room, membership, directory, and profile tools](usage.md#mcp-tools), with text-only `matrix_send_message`.
Use raw Matrix room and event IDs.
Numeric references and local-file upload remain stdio features.
Both transports also expose [history, message actions, membership, media, and catch-up tools](usage.md#history-and-message-context) using raw Matrix IDs.
Hosted file transfers use base64 payloads with a 5 MiB decoded limit, not server filesystem paths.
Downloads use authenticated homeserver media routes and refuse redirects.
Each hosted tool opens its own Matrix client with the request's verified credential and closes it after the call.
Invitations and room updates require the connected account's normal Matrix permissions.
Profile setters affect only that account's global display name or avatar.
Avatar updates use existing `mxc://` media URIs.
Use `matrix_upload_media` to obtain one for a new image.
Read tools carry MCP read-only hints; invitations and updates are marked as mutations.
Catch-up reads leave read state unchanged; `matrix_mark_read` requires an explicit call, clears a manual unread flag, and defaults to a private receipt.
Invitation and catch-up offsets page a fresh filtered sync snapshot after download.
The snapshot has a 2 MiB JSON limit; smaller output page limits do not reduce upstream snapshot bytes.
Edits and redactions target only the connected user's events; room creation defaults to private, unencrypted rooms.

Address a specific agent or user by passing full Matrix user IDs in `mentions`.
For a threaded request, reuse the root event ID when reading the reply:

```text
matrix_send_message(
    room_id="!room:example.com",
    body="Could you check this?",
    thread_id="$root",
    mentions=["@helper:example.com"],
)
matrix_read_thread(room_id="!room:example.com", thread_id="$root")
```

End-to-end encryption is unsupported; hosted sends transmit plaintext.
Before each send, a best effort preflight checks `m.room.encryption` and refuses known encrypted rooms or any lookup result other than a definitive missing encryption state event.
The check and send are separate, non-atomic operations: a room can enable encryption between them and still receive the plaintext message.
Hosted sends provide no E2EE guarantee.
Do not use them where end-to-end encryption is required.

No administration, account provisioning, administrator credentials, or appservice credentials are provided.
