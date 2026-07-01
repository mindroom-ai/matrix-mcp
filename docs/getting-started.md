---
icon: lucide/rocket
---

# Getting Started

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

### Existing Matrix Access Token

```bash
matrix-mcp auth token https://mindroom.chat @alice:mindroom.chat "$MATRIX_ACCESS_TOKEN" --device-id DEVICEID
```

### Password Auth

```bash
matrix-mcp auth password https://mindroom.chat @alice:mindroom.chat
```

## Configure an MCP Client

=== "Claude Code"

    ```bash
    claude mcp add matrix -- matrix-mcp serve
    ```

=== "Codex"

    ```bash
    codex mcp add matrix -- matrix-mcp serve
    ```


The server runs over stdio and does not expose a local HTTP port during normal MCP operation.

## Verify

Ask the MCP client to call `matrix_whoami`.
It should return the Matrix user and device saved by the login command.
