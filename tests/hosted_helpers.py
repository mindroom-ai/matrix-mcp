from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import web

if TYPE_CHECKING:
    import httpx

CALLBACK = "https://client.example.com/callback"
VERIFIER = "test-pkce-verifier-with-at-least-forty-three-characters"


@dataclass
class FakeMatrix:
    sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    refreshes: dict[str, str] = field(default_factory=dict)
    logins: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    logout_fails: bool = False
    legacy: bool = False
    expires_in_ms: int = 3_600_000
    encryption_status: int = 404

    def login_token(self, user: str) -> str:
        token = secrets.token_urlsafe(24)
        self.logins[token] = user
        return token

    def invalidate(self, user: str) -> None:
        for token, identity in list(self.sessions.items()):
            if identity[0] == f"@{user}:example.com":
                del self.sessions[token]

    def session(self, user: str, device: str | None = None) -> dict[str, Any]:
        access = f"matrix-access-{secrets.token_urlsafe(24)}"
        device = device or secrets.token_hex(8)
        identity = (f"@{user}:example.com", device)
        self.sessions[access] = identity
        result: dict[str, Any] = {
            "access_token": access,
            "user_id": identity[0],
            "device_id": device,
        }
        if not self.legacy:
            refresh = f"matrix-refresh-{secrets.token_urlsafe(24)}"
            self.refreshes[refresh] = access
            result.update(refresh_token=refresh, expires_in_ms=self.expires_in_ms)
        return result

    async def handle(self, request: web.Request) -> web.Response:  # noqa: C901, PLR0911, PLR0912 - Fake HTTP API dispatch.
        path = request.path
        if path.endswith("/login") and request.method == "POST":
            data = await request.json()
            assert data["type"] == "m.login.token"
            assert data["refresh_token"] is True
            user = self.logins.pop(data["token"], None)
            if user:
                return web.json_response(self.session(user))
            return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
        if path.endswith("/refresh"):
            data = await request.json()
            access = self.refreshes.pop(data["refresh_token"], None)
            identity = self.sessions.get(access or "")
            if identity:
                result = self.session(identity[0].split(":")[0][1:], identity[1])
                self.sessions.pop(access or "")
                return web.json_response(result)
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        access = request.headers.get("Authorization", "").removeprefix("Bearer ")
        identity = self.sessions.get(access)
        if not identity:
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        if path.endswith("/account/whoami"):
            return web.json_response({"user_id": identity[0], "device_id": identity[1]})
        if path.endswith("/logout"):
            if self.logout_fails:
                return web.json_response({"errcode": "M_UNKNOWN"}, status=503)
            for token, other in list(self.sessions.items()):
                if other == identity:
                    self.sessions.pop(token)
            return web.json_response({})
        if path.endswith("/joined_rooms"):
            return web.json_response({"joined_rooms": ["!room:example.com"]})
        if "/state/m.room.name" in path:
            return web.json_response({"name": "Example room"})
        if "/state/m.room.encryption" in path:
            data = (
                {"algorithm": "m.megolm.v1.aes-sha2"}
                if self.encryption_status == 200
                else {"errcode": "M_NOT_FOUND"}
            )
            return web.json_response(data, status=self.encryption_status)
        if "/send/m.room.message/" in path:
            content = await request.json()
            self.messages.append({"sender": identity[0], "content": content, "path": path})
            return web.json_response({"event_id": f"$event{len(self.messages)}"})
        if path.endswith("/messages"):
            return web.json_response({"chunk": [self.event()], "start": "start", "end": "end"})
        if "/event/" in path:
            return web.json_response(self.event())
        if "/relations/" in path:
            return web.json_response({"chunk": []})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    @staticmethod
    def event() -> dict[str, Any]:
        return {
            "type": "m.room.message",
            "event_id": "$root",
            "sender": "@alice:example.com",
            "origin_server_ts": 1000,
            "content": {"msgtype": "m.text", "body": "Hello"},
        }


@dataclass
class OAuthBrowser:
    client: httpx.AsyncClient
    matrix: FakeMatrix

    async def register(self, redirect: str = CALLBACK) -> str:
        response = await self.client.post(
            "/register",
            json={
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "matrix",
                "client_name": "Example client",
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["client_id"])

    async def consent(self, client_id: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
        response = await self.client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "response_type": "code",
                "scope": "matrix",
                "state": "client-state",
                "code_challenge_method": "S256",
                "code_challenge": challenge.decode().rstrip("="),
            },
        )
        assert response.status_code == 302, response.text
        response = await self.client.get(response.headers["location"])
        if response.status_code == 200:
            fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', response.text))
            response = await self.client.post(
                "/consent",
                data={
                    "txn_id": fields["txn_id"],
                    "csrf_token": fields["csrf_token"],
                    "action": "approve",
                },
            )
        assert response.status_code == 302, response.text
        upstream = urlsplit(response.headers["location"])
        assert upstream.path == "/_matrix/client/v3/login/sso/redirect"
        return parse_qs(upstream.query)["redirectUrl"][0]

    async def code(self, client_id: str, user: str = "alice") -> str:
        callback = await self.consent(client_id)
        response = await self.client.get(
            callback
            + "&"
            + urlencode(
                {
                    "loginToken": self.matrix.login_token(user),
                }
            )
        )
        assert response.status_code == 302, response.text
        params = parse_qs(urlsplit(response.headers["location"]).query)
        assert params["state"] == ["client-state"]
        return params["code"][0]

    async def exchange(self, client_id: str, code: str, verifier: str = VERIFIER) -> httpx.Response:
        return await self.client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": CALLBACK,
                "code_verifier": verifier,
            },
        )

    async def login(self, client_id: str, user: str = "alice") -> dict[str, Any]:
        response = await self.exchange(client_id, await self.code(client_id, user))
        assert response.status_code == 200, response.text
        tokens: dict[str, Any] = response.json()
        assert tokens["scope"] == "matrix"
        assert 0 < tokens["expires_in"] <= 3600
        assert tokens["refresh_token"]
        assert all(value not in response.text for value in self.matrix.sessions)
        assert all(value not in response.text for value in self.matrix.refreshes)
        return tokens

    async def refresh(self, client_id: str, token: str) -> httpx.Response:
        return await self.client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token,
            },
        )

    async def revoke(self, client_id: str, token: str) -> None:
        response = await self.client.post(
            "/revoke",
            data={
                "client_id": client_id,
                "client_secret": "",
                "token": token,
            },
        )
        assert response.status_code == 200, response.text

    async def rpc(self, token: str, method: str, params: dict[str, Any]) -> httpx.Response:
        return await self.client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )

    async def call(self, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.rpc(token, "tools/call", {"name": name, "arguments": arguments})
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()["result"]
        assert not result.get("isError"), result
        return result
