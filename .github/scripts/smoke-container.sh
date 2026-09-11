#!/usr/bin/env bash
# Exercise the packaged HTTP server without a real Matrix account or homeserver.
set -euo pipefail
image="${1:?Usage: smoke-container.sh IMAGE}"
container="matrix-mcp-smoke-$$"
cleanup() {
    docker logs "$container" || true
    docker rm -f "$container" >/dev/null || true
    docker volume rm "$container" >/dev/null || true
}
trap cleanup EXIT

docker run --detach --name "$container" --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges \
    --mount "type=volume,src=$container,dst=/data" \
    --publish 127.0.0.1::8000 \
    --env MATRIX_MCP_HOSTED_SECRET_KEY=container-smoke-test-key-never-use-outside-tests \
    "$image" serve --transport http --host 0.0.0.0 \
    --public-base-url https://mcp.example.com \
    --homeserver https://matrix.example.com --state-directory /data/oauth \
    --allowed-client-redirect-uri https://client.example.com/oauth/callback >/dev/null
test "$(docker exec "$container" id -u)" != 0

check_http() {
    local origin
    origin="http://$(docker port "$container" 8000/tcp)"
    curl --fail --silent --show-error --retry 20 --retry-delay 1 \
        --retry-all-errors --max-time 5 \
        "$origin/.well-known/oauth-authorization-server" \
        | jq -e '.issuer == "https://mcp.example.com/" and .token_endpoint == "https://mcp.example.com/token"' >/dev/null
    test "$(curl --silent --show-error --max-time 5 --output /dev/null \
        --write-out '%{http_code}' --request POST \
        --header 'Content-Type: application/json' \
        --header 'Accept: application/json, text/event-stream' \
        --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
        "$origin/mcp")" = 401
}
check_http
docker restart "$container" >/dev/null
check_http
