---
icon: lucide/rocket
---

# Getting Started

Three steps: install the CLI, log in to your homeserver, and register the server with your MCP client.

## Prerequisites

You need:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- an MCP client such as Claude Code or Codex
- a Matrix homeserver account

## Installation

=== "uv tool"

    ```bash
    uv tool install matrix-mcp
    ```

=== "pipx"

    ```bash
    pipx install matrix-mcp
    ```

=== "pip"

    ```bash
    pip install matrix-mcp
    ```

=== "From source"

    ```bash
    git clone https://github.com/mindroom-ai/matrix-mcp.git
    cd matrix-mcp
    uv sync --extra dev
    ```

## Login

### Matrix SSO

```bash
matrix-mcp auth sso https://mindroom.chat
```

If the homeserver advertises multiple SSO providers, list them:

```bash
matrix-mcp auth providers https://mindroom.chat
```

Then pass the provider ID explicitly:

```bash
matrix-mcp auth sso https://mindroom.chat --idp-id github
```

### SSO over SSH or on a Headless Machine

The SSO flow starts a temporary callback server on the machine running `matrix-mcp` and waits for the browser to be redirected to it.
If that machine is remote — an SSH session, a VM, a container — a browser on your local machine cannot reach the callback address printed in the SSO URL.

=== "SSH port forwarding"

    Pin the callback port on the remote machine:

    ```bash
    matrix-mcp auth sso https://mindroom.chat --callback-port 8765
    ```

    While that command waits, forward the port from your local machine in a second terminal:

    ```bash
    ssh -N -L 8765:127.0.0.1:8765 remote-host
    ```

    Open the printed SSO URL in your local browser.
    After login, the homeserver redirects to `http://127.0.0.1:8765/callback`, which SSH forwards to the waiting command on the remote machine.

=== "Manual token exchange"

    If port forwarding is not an option, print the SSO URL with a placeholder redirect URL:

    ```bash
    matrix-mcp auth sso-url https://mindroom.chat http://127.0.0.1:8765/callback
    ```

    Open that URL in any browser and log in.
    The final redirect to `http://127.0.0.1:8765/callback?loginToken=...` fails to load — that is expected.
    Copy the `loginToken` value from the browser address bar and exchange it on the remote machine right away (login tokens are single-use and expire within minutes):

    ```bash
    matrix-mcp auth login-token https://mindroom.chat syt_...
    ```

=== "Copy credentials"

    If `matrix-mcp` is also installed on the machine with the browser, log in there:

    ```bash
    matrix-mcp auth sso https://mindroom.chat
    matrix-mcp config-path
    ```

    Then copy the file printed by `config-path` to the path that `matrix-mcp config-path` prints on the remote machine, creating the directory if needed.

    !!! warning "No encrypted rooms with copied credentials"

        The device's encryption keys stay on the machine that logged in, and the remote machine refuses to publish new ones for the same device.
        Use one of the other methods when you need encrypted rooms on the remote machine.

### Existing Matrix Access Token

```bash
matrix-mcp auth token https://mindroom.chat @alice:mindroom.chat "$MATRIX_ACCESS_TOKEN" --device-id DEVICEID
```

### Password Auth

```bash
matrix-mcp auth password https://mindroom.chat @alice:mindroom.chat
```

### End-to-End Encryption

SSO, password, and login-token logins create a new Matrix device for Matrix MCP and publish its encryption keys, so encrypted rooms work from the start.
`auth token` reuses the device that the access token belongs to; encryption then works only if no other client has published keys for that device.
Messages sent before the login cannot be decrypted unless you import room keys exported from another client:

```bash
matrix-mcp e2ee import-keys element-keys.txt
```

See [End-to-End Encryption](encryption.md) for details.

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

### Log Out

```bash
matrix-mcp auth logout
```

Logout removes the stored credentials and the device's end-to-end encryption keys.

## Configure an MCP Client

=== "Claude Code"

    ```bash
    claude mcp add matrix -- matrix-mcp serve
    ```

=== "Codex"

    ```bash
    codex mcp add matrix -- matrix-mcp serve
    ```

=== "Any MCP client"

    Register a stdio server that runs:

    ```bash
    matrix-mcp serve
    ```

The server runs over stdio and does not expose a local HTTP port during normal MCP operation.
To serve remote clients instead, see [Authenticated HTTP](hosted.md).

## Verify

Ask the MCP client to call `matrix_whoami`.
It should return the Matrix user and device saved by the login command.

Then try a few real requests:

- *"List my Matrix rooms."*
- *"What did I miss? Check my unread rooms."*
- *"Summarize the latest thread in the project room."*

The [Tool Reference](usage.md) covers everything the agent can do.

## Command Reference

| Command | What it does |
| --- | --- |
| `matrix-mcp auth sso <homeserver>` | Log in through Matrix SSO in a browser |
| `matrix-mcp auth providers <homeserver>` | List the homeserver's SSO provider IDs |
| `matrix-mcp auth sso-url <homeserver> <redirect-url>` | Print an SSO URL for a manual login |
| `matrix-mcp auth login-token <homeserver> <token>` | Exchange an SSO `loginToken` for a session |
| `matrix-mcp auth password <homeserver> <user-id>` | Log in with a password |
| `matrix-mcp auth token <homeserver> <user-id> <token>` | Store an existing access token |
| `matrix-mcp auth logout` | Remove stored credentials and encryption keys |
| `matrix-mcp e2ee setup` | Publish this device's encryption keys and show its fingerprint |
| `matrix-mcp e2ee import-keys <file>` | Import room keys exported from another client |
| `matrix-mcp serve` | Run the MCP server (stdio by default) |
| `matrix-mcp config-path` | Print where credentials are stored |

Run any command with `--help` for its options.
