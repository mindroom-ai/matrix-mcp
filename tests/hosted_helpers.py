from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import web

if TYPE_CHECKING:
    import httpx
    from starlette.types import ASGIApp, Message, Scope

CALLBACK = "https://client.example.com/callback"
VERIFIER = "test-pkce-verifier-with-at-least-forty-three-characters"


@dataclass
class FakeMatrix:
    sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    refreshes: dict[str, str] = field(default_factory=dict)
    logins: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    invitations: list[dict[str, str]] = field(default_factory=list)
    room_writes: list[dict[str, Any]] = field(default_factory=list)
    conversation_requests: list[dict[str, Any]] = field(default_factory=list)
    media_store: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    profiles: dict[str, dict[str, str | None]] = field(default_factory=dict)
    room_state: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    logout_fails: bool = False
    legacy: bool = False
    expires_in_ms: float | str | None = 3_600_000
    encryption_status: int = 404
    omit_replacement_refresh: bool = False
    expired_access: set[str] = field(default_factory=set)
    refresh_started: asyncio.Event | None = None
    refresh_release: asyncio.Event | None = None
    whoami_started: asyncio.Event | None = None
    whoami_release: asyncio.Event | None = None

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
        self.profiles.setdefault(identity[0], {"displayname": user, "avatar_url": None})
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

    async def handle(self, request: web.Request) -> web.Response:  # noqa: C901, PLR0911, PLR0912, PLR0915 - Fake HTTP API dispatch.
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
            if self.refresh_started is not None and self.refresh_release is not None:
                self.refresh_started.set()
                await self.refresh_release.wait()
            access = self.refreshes.pop(data["refresh_token"], None)
            identity = self.sessions.get(access or "")
            if identity:
                result = self.session(identity[0].split(":")[0][1:], identity[1])
                self.sessions.pop(access or "")
                if self.omit_replacement_refresh:
                    self.refreshes.pop(result.pop("refresh_token"))
                    self.refreshes[data["refresh_token"]] = result["access_token"]
                return web.json_response(result)
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        access = request.headers.get("Authorization", "").removeprefix("Bearer ")
        identity = self.sessions.get(access)
        if not identity or access in self.expired_access:
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        if path.endswith("/account/whoami"):
            if self.whoami_started is not None and self.whoami_release is not None:
                self.whoami_started.set()
                await self.whoami_release.wait()
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
        management = await self.room_profile_request(request, identity[0])
        if management is not None:
            return management
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
            if content.get("url") is not None:
                self._record_conversation("send_media", identity[0], path)
            elif (
                isinstance(content.get("m.relates_to"), dict)
                and "m.in_reply_to" in content["m.relates_to"]
            ):
                self._record_conversation("reply", identity[0], path)
            return web.json_response({"event_id": f"$event{len(self.messages)}"})
        if "/send/m.reaction/" in path:
            self._record_conversation("react", identity[0], path)
            return web.json_response({"event_id": f"$reaction{len(self.conversation_requests)}"})
        if "/redact/" in path and request.method == "PUT":
            self._record_conversation("redact", identity[0], path)
            return web.json_response({"event_id": f"$redaction{len(self.conversation_requests)}"})
        if path.endswith("/messages"):
            self._record_conversation("history", identity[0], path)
            return web.json_response({"chunk": [self.event()], "start": "start", "end": "end"})
        if "/context/" in path:
            self._record_conversation("context", identity[0], path)
            return web.json_response(
                {"event": self.event(), "events_before": [], "events_after": []}
            )
        if "/event/" in path:
            return web.json_response(self.event())
        if "/relations/" in path:
            return web.json_response({"chunk": []})
        if path.endswith("/sync"):
            self._record_conversation("unread", identity[0], path)
            return web.json_response({"next_batch": "sync", "rooms": {}})
        if "/join/" in path and request.method == "POST":
            self._record_conversation("join", identity[0], path)
            return web.json_response({"room_id": "!joined:example.com"})
        if path.endswith("/createRoom") and request.method == "POST":
            self._record_conversation("create", identity[0], path)
            return web.json_response({"room_id": "!created:example.com"})
        if path.endswith("/_matrix/media/v3/upload") and request.method == "POST":
            data = await request.read()
            media_id = f"media-{len(self.media_store) + 1}"
            content_type = request.headers.get("Content-Type", "application/octet-stream")
            self.media_store[media_id] = (data, content_type)
            self._record_conversation("upload", identity[0], path)
            return web.json_response({"content_uri": f"mxc://example.com/{media_id}"})
        if "/_matrix/client/v1/media/download/" in path and request.method == "GET":
            media_id = path.rsplit("/", 1)[-1]
            media = self.media_store.get(media_id)
            if media is None:
                return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
            self._record_conversation("download", identity[0], path)
            return web.Response(body=media[0], headers={"Content-Type": media[1]})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    def _record_conversation(self, operation: str, actor: str, path: str) -> None:
        self.conversation_requests.append({"operation": operation, "actor": actor, "path": path})

    async def room_profile_request(  # noqa: C901, PLR0911, PLR0912 - Fake API routes.
        self, request: web.Request, actor: str
    ) -> web.Response | None:
        path = request.path.rstrip("/")
        if "/rooms/" in path:
            room_id = path.split("/rooms/", 1)[1].split("/", 1)[0]
            if room_id == "!forbidden:example.com":
                return web.json_response(
                    {"errcode": "M_FORBIDDEN", "error": "Room access denied"}, status=403
                )
            if path.endswith("/joined_members") and request.method == "GET":
                return web.json_response(
                    {
                        "joined": {
                            user_id: {
                                "display_name": profile["displayname"],
                                "avatar_url": profile["avatar_url"],
                            }
                            for user_id, profile in reversed(list(self.profiles.items()))
                        }
                    }
                )
            if path.endswith("/invite") and request.method == "POST":
                content = await request.json()
                self.invitations.append(
                    {"actor": actor, "room_id": room_id, "user_id": content["user_id"]}
                )
                return web.json_response({})
            if "/state/" in path:
                event_type = path.split("/state/", 1)[1]
                if event_type in {"m.room.name", "m.room.topic", "m.room.avatar"}:
                    if request.method == "PUT":
                        content = await request.json()
                        self.room_state[room_id, event_type] = content
                        self.room_writes.append(
                            {"actor": actor, "type": event_type, "content": content}
                        )
                        return web.json_response({"event_id": f"$state{len(self.room_writes)}"})
                    if request.method == "GET":
                        content = self.room_state.get((room_id, event_type))
                        if content is None and event_type == "m.room.name":
                            content = {"name": "Example room"}
                        if content is not None:
                            return web.json_response(content)
                        return web.json_response(
                            {"errcode": "M_NOT_FOUND", "error": "No state event"}, status=404
                        )
        if "/profile/" in path:
            parts = path.split("/profile/", 1)[1].split("/")
            user_id = parts[0]
            if request.method == "GET" and len(parts) == 1:
                return web.json_response(
                    {
                        key: value
                        for key, value in self.profiles.get(user_id, {}).items()
                        if value is not None
                    }
                )
            if request.method == "PUT" and len(parts) == 2:
                if user_id != actor:
                    return web.json_response({"errcode": "M_FORBIDDEN"}, status=403)
                content = await request.json()
                self.profiles[actor].update(content)
                return web.json_response({})
        if path.endswith("/user_directory/search") and request.method == "POST":
            content = await request.json()
            matches = [
                {
                    "user_id": user_id,
                    "display_name": profile["displayname"],
                    "avatar_url": profile["avatar_url"],
                }
                for user_id, profile in self.profiles.items()
                if content["search_term"].lower() in user_id.lower()
            ]
            limit = content["limit"]
            return web.json_response({"results": matches[:limit], "limited": len(matches) > limit})
        return None

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
    asgi_app: ASGIApp | None = None

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

    async def authorize(self, client_id: str, redirect: str = CALLBACK) -> httpx.Response:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
        return await self.client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": "matrix",
                "state": "client-state",
                "code_challenge_method": "S256",
                "code_challenge": challenge.decode().rstrip("="),
            },
        )

    async def consent(self, client_id: str) -> str:
        response = await self.authorize(client_id)
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


@dataclass
class PausedOAuthRequest:
    """A single local ASGI exchange with one explicitly controlled I/O boundary."""

    phase: Literal["receive", "send"]
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    messages: list[Message] = field(default_factory=list)

    async def run(self, app: ASGIApp) -> None:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/register",
            "raw_path": b"/register",
            "root_path": "",
            "query_string": b"",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 8765),
            "headers": [(b"content-type", b"application/json")],
        }
        body = json.dumps(
            {
                "redirect_uris": [CALLBACK],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
            }
        ).encode()

        async def receive() -> Message:
            if self.phase == "receive":
                self.blocked.set()
                await self.release.wait()
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Message) -> None:
            if self.phase == "send" and message["type"] == "http.response.start":
                self.blocked.set()
                await self.release.wait()
            self.messages.append(message)

        await app(scope, receive, send)
