---
icon: lucide/rocket
---

# Getting Started

## Prerequisites

You need:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- an MCP client such as Claude Code
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
matrix-mcp auth sso https://matrix.example.com
```

If the homeserver advertises multiple SSO providers, list them:

```bash
matrix-mcp auth providers https://matrix.example.com
```

Then pass the provider ID explicitly:

```bash
matrix-mcp auth sso https://matrix.example.com --idp-id github
```

### Existing Matrix Access Token

```bash
matrix-mcp auth token https://matrix.example.com @alice:example.com "$MATRIX_ACCESS_TOKEN" --device-id DEVICEID
```

### Password Auth

```bash
matrix-mcp auth password https://matrix.example.com @alice:example.com
```

## Configure Claude Code

```bash
claude mcp add matrix -- matrix-mcp serve
```

The server runs over stdio and does not expose a local HTTP port during normal MCP operation.

## Verify

Ask the MCP client to call `matrix_whoami`.
It should return the Matrix user and device saved by the login command.
