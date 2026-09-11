"""Matrix SSO adapter for FastMCP's OAuth proxy and encrypted token store."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path  # noqa: TC003 - Pydantic resolves settings annotations at runtime.
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from authlib.jose.errors import JoseError
from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth import AccessToken, OAuthProxy, TokenVerifier
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from filelock import FileLock, Timeout
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import AccessToken as SDKAccessToken
from mcp.server.auth.provider import RefreshToken, RegistrationError
from mcp.server.auth.routes import build_metadata, cors_middleware
from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from authlib.integrations.httpx_client import AsyncOAuth2Client
    from fastmcp import FastMCP
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from mcp.shared.auth import OAuthClientInformationFull
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ACCESS_TTL = 3600
_REFRESH_TTL = 30 * 24 * 3600
_MIN_KEY_LENGTH = 32
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _validated_url(value: str, *, public: bool = True) -> str:
    normalized = str(AnyHttpUrl(value))
    url = urlsplit(normalized)
    if url.username or url.password or url.fragment or url.query or "*" in value:
        msg = "URLs must not contain credentials, query strings, fragments, or wildcards"
        raise ValueError(msg)
    if public and url.scheme != "https" and url.hostname not in _LOOPBACK:
        msg = "Public URLs require HTTPS, except for loopback development"
        raise ValueError(msg)
    return normalized


class HostedSettings(BaseSettings):
    """Explicit hosted configuration, independent of local Matrix credentials."""

    model_config = SettingsConfigDict(
        env_prefix="MATRIX_MCP_HOSTED_",
        frozen=True,
        hide_input_in_errors=True,
    )

    public_base_url: str
    homeserver: str
    api_base_url: str | None = None
    state_directory: Path
    secret_key: str = Field(min_length=_MIN_KEY_LENGTH, repr=False, exclude=True)
    allowed_client_redirect_uris: list[str] = Field(min_length=1)

    @field_validator("public_base_url", "homeserver")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        return _validated_url(value).rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        if urlsplit(value).path:
            msg = "The public MCP base URL must be an origin without a path"
            raise ValueError(msg)
        return value

    @field_validator("api_base_url")
    @classmethod
    def validate_api_url(cls, value: str | None) -> str | None:
        return _validated_url(value, public=False).rstrip("/") if value is not None else None

    @field_validator("secret_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if len(value.strip()) < _MIN_KEY_LENGTH:
            msg = "The signing key must contain at least 32 non-padding characters"
            raise ValueError(msg)
        return value

    @field_validator("allowed_client_redirect_uris")
    @classmethod
    def validate_callbacks(cls, values: list[str]) -> list[str]:
        return [_validated_url(value) for value in values]

    @property
    def matrix_api_url(self) -> str:
        return self.api_base_url or self.homeserver


class MatrixWhoamiVerifier(TokenVerifier):
    def __init__(self, api_url: str) -> None:
        super().__init__(required_scopes=["matrix"])
        self.api_url = api_url

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.get(
                    f"{self.api_url}/_matrix/client/v3/account/whoami",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                identity = response.json()
            user_id, device_id = identity.get("user_id"), identity.get("device_id")
            if not isinstance(user_id, str) or not isinstance(device_id, str):
                return None
            if not user_id.startswith("@") or not device_id:
                return None
            return AccessToken(
                token=token,
                client_id="matrix-session",
                scopes=["matrix"],
                claims={"user_id": user_id, "device_id": device_id},
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None


class MatrixTokenClient:
    """Only the two upstream client hooks OAuthProxy calls; no OAuth grant server."""

    def __init__(self, settings: HostedSettings, fernet: Fernet) -> None:
        self.settings = settings
        self.fernet = fernet
        self.verifier = MatrixWhoamiVerifier(settings.matrix_api_url)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    f"{self.settings.matrix_api_url}/_matrix/client/v3/{path}",
                    json=body,
                )
                response.raise_for_status()
                result = response.json()
            if not isinstance(result, dict):
                raise TypeError  # noqa: TRY301 - Normalize malformed upstream responses.
            return cast("dict[str, Any]", result)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # OAuthProxy displays upstream exceptions. Never include Matrix response bodies.
            msg = "Matrix session exchange failed"
            raise ValueError(msg) from exc

    async def _tokens(
        self,
        data: dict[str, Any],
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        access = data.get("access_token")
        validated = await self.verifier.verify_token(access) if isinstance(access, str) else None
        if validated is None or (expected is not None and validated.claims != expected):
            msg = "Matrix session identity validation failed"
            raise ValueError(msg)
        # Wrap genuine refresh tokens and legacy sessions identically. The wrapper
        # stays inside the encrypted upstream store, never in client-facing JWTs.
        private_session = {
            "access_token": access,
            "refresh_token": data.get("refresh_token"),
            "identity": validated.claims,
        }
        expires_in = min(_ACCESS_TTL, int(data.get("expires_in_ms", _ACCESS_TTL * 1000)) // 1000)
        if expires_in <= 0:
            msg = "Matrix access token expires too soon"
            raise ValueError(msg)
        return {
            "access_token": access,
            "refresh_token": self.fernet.encrypt(json.dumps(private_session).encode()).decode(),
            "expires_in": expires_in,
            "refresh_expires_in": _REFRESH_TTL,
            "scope": "matrix",
            "token_type": "Bearer",
        }

    async def fetch_token(self, *, code: str, **_kwargs: Any) -> dict[str, Any]:
        data = await self._post(
            "login",
            {
                "type": "m.login.token",
                "token": code,
                "refresh_token": True,
                "initial_device_display_name": "Matrix MCP",
            },
        )
        return await self._tokens(data)

    async def refresh_token(self, *, refresh_token: str, **_kwargs: Any) -> dict[str, Any]:
        try:
            session = json.loads(self.fernet.decrypt(refresh_token.encode()))
            if session["refresh_token"]:
                data = await self._post("refresh", {"refresh_token": session["refresh_token"]})
            else:
                data = {"access_token": session["access_token"]}
            return await self._tokens(data, expected=session["identity"])
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            msg = "Matrix session renewal failed"
            raise ValueError(msg) from exc


class _SerializedAuthRoute:
    """Serialize complete handlers, including framework load/consume sequences."""

    def __init__(self, app: ASGIApp, lock: asyncio.Lock) -> None:
        self.app, self.lock = app, lock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with self.lock:
            await self.app(scope, receive, send)


class _PublicClientRevocationRoute:
    """Supply the SDK's required nullable secret field for public clients."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if request.method != "POST" or not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            await self.app(scope, receive, send)
            return
        body = await request.body()
        if "client_secret" not in parse_qs(body.decode("latin-1"), keep_blank_values=True):
            body += b"&client_secret="
        forwarded = dict(scope)
        forwarded["headers"] = [
            (key, value) for key, value in scope["headers"] if key.lower() != b"content-length"
        ] + [(b"content-length", str(len(body)).encode())]
        consumed = False

        async def replay() -> Message:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(forwarded, replay, send)


class MatrixOAuthProvider(OAuthProxy):
    def __init__(self, settings: HostedSettings) -> None:
        self.settings = settings
        self._auth_lock = asyncio.Lock()
        self._fernet = Fernet(
            derive_jwt_key(
                high_entropy_material=settings.secret_key,
                salt="matrix-mcp-hosted-storage",
            )
        )
        directory = settings.state_directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        store = FernetEncryptionWrapper(
            FileTreeStore(data_directory=directory / "oauth"),
            fernet=self._fernet,
            raise_on_decryption_error=True,
        )
        super().__init__(
            base_url=settings.public_base_url,
            upstream_authorization_endpoint=f"{settings.homeserver}/_matrix/client/v3/login/sso/redirect",
            upstream_token_endpoint=f"{settings.matrix_api_url}/_matrix/client/v3/login",
            upstream_revocation_endpoint=f"{settings.matrix_api_url}/_matrix/client/v3/logout",
            upstream_client_id="matrix-mcp",
            token_verifier=MatrixWhoamiVerifier(settings.matrix_api_url),
            allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
            valid_scopes=["matrix"],
            forward_pkce=False,
            forward_resource=False,
            client_storage=store,
            jwt_signing_key=derive_jwt_key(
                high_entropy_material=settings.secret_key,
                salt="matrix-mcp-hosted-signing",
            ),
            enable_cimd=False,
            require_authorization_consent=True,
        )

    def _open_state(self) -> FileLock:
        lock = FileLock(self.settings.state_directory / "server.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            msg = "Hosted state directory is already in use by another server"
            raise RuntimeError(msg) from exc
        marker = self.settings.state_directory / "identity.enc"
        binding = json.dumps(
            [
                self.settings.public_base_url,
                self.settings.homeserver,
                self.settings.matrix_api_url,
            ]
        ).encode()
        try:
            if marker.exists():
                if self._fernet.decrypt(marker.read_bytes()) != binding:
                    raise InvalidToken  # noqa: TRY301 - Release the state lock on failure.
            else:
                with marker.open("xb") as file:
                    file.write(self._fernet.encrypt(binding))
                marker.chmod(0o600)
        except (InvalidToken, OSError) as exc:
            lock.release()
            msg = "Hosted state key or configuration does not match; restore the original settings"
            raise RuntimeError(msg) from exc
        return lock

    @asynccontextmanager
    async def lifespan(self, _server: FastMCP) -> AsyncIterator[dict[str, object]]:
        lock = self._open_state()
        try:
            yield {}
        finally:
            lock.release()

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        for route in routes:
            if route.path.startswith("/.well-known/oauth-authorization-server"):
                metadata = build_metadata(
                    AnyHttpUrl(self.settings.public_base_url),
                    self.service_documentation_url,
                    cast("ClientRegistrationOptions", self.client_registration_options),
                    cast("RevocationOptions", self.revocation_options),
                )
                metadata.token_endpoint_auth_methods_supported = ["none"]
                metadata.revocation_endpoint_auth_methods_supported = ["none"]
                route.app = Route(
                    route.path,
                    endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
                    methods=["GET", "OPTIONS"],
                ).app
            if route.path == "/revoke":
                route.app = _PublicClientRevocationRoute(route.app)
            route.app = _SerializedAuthRoute(route.app, self._auth_lock)
        return routes

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        allowed = self.settings.allowed_client_redirect_uris
        if not client_info.redirect_uris or any(
            str(uri) not in allowed for uri in client_info.redirect_uris
        ):
            msg = "Client callback is not allowed"
            raise RegistrationError(error="invalid_redirect_uri", error_description=msg)
        if client_info.token_endpoint_auth_method != "none":  # noqa: S105 - OAuth method name.
            msg = "Use public-client PKCE authentication"
            raise RegistrationError(error="invalid_client_metadata", error_description=msg)
        await super().register_client(client_info)

    def _create_upstream_oauth_client(self) -> AsyncOAuth2Client:
        # FastMCP annotates a concrete Authlib client but consumes only these hooks.
        return cast("AsyncOAuth2Client", MatrixTokenClient(self.settings, self._fernet))

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict[str, Any]) -> str:  # noqa: ARG002 - Framework hook signature.
        callback = f"{self.settings.public_base_url}/auth/callback?{urlencode({'state': txn_id})}"
        return f"{self._upstream_authorization_endpoint}?{urlencode({'redirectUrl': callback})}"

    async def _handle_idp_callback(self, request: Request) -> HTMLResponse | RedirectResponse:
        query = request.query_params
        if any(len(query.getlist(key)) != 1 or not query[key] for key in ("state", "loginToken")):
            return HTMLResponse("Invalid Matrix login callback", status_code=400)
        # Preserve headers/cookies and framework transaction checks. Translate only
        # Matrix's loginToken parameter to the OAuthProxy upstream code hook.
        scope = dict(request.scope)
        scope["query_string"] = urlencode(
            {"state": query["state"], "code": query["loginToken"]}
        ).encode()
        return await super()._handle_idp_callback(Request(scope, request.receive))

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            payload = self.jwt_issuer.verify_token(token)
            if payload.get("scope", "").split() != ["matrix"]:
                return None
            mapping = await self._jti_mapping_store.get(key=payload["jti"])
            upstream = (
                await self._upstream_token_store.get(key=mapping.upstream_token_id)
                if mapping
                else None
            )
            if upstream is None or upstream.client_id != payload["client_id"]:
                return None
            validated = await self._token_validator.verify_token(upstream.access_token)
            if validated is None:
                return None
            # Keep Matrix token for tools; keep MCP client and lineage for revoke.
            return validated.model_copy(
                update={
                    "client_id": upstream.client_id,
                    "scopes": ["matrix"],
                    "expires_at": int(payload["exp"]),
                    "claims": {**validated.claims, "lineage": upstream.upstream_token_id},
                }
            )
        except (JoseError, KeyError, ValueError, TypeError):
            return None

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        loaded = await super().load_refresh_token(client, refresh_token)
        if loaded is None or loaded.client_id != client.client_id:
            return None
        try:
            payload = self.jwt_issuer.verify_token(refresh_token, expected_token_use="refresh")  # noqa: S106 - JWT purpose, not a credential.
            mapping = await self._jti_mapping_store.get(key=payload["jti"])
            upstream = (
                await self._upstream_token_store.get(key=mapping.upstream_token_id)
                if mapping
                else None
            )
        except (JoseError, KeyError, ValueError, TypeError):
            return None
        return loaded if upstream is not None and upstream.client_id == client.client_id else None

    async def revoke_token(self, token: SDKAccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            payload = self.jwt_issuer.verify_token(token.token, expected_token_use="refresh")  # noqa: S106 - JWT purpose, not a credential.
            mapping = await self._jti_mapping_store.get(key=payload["jti"])
            lineage = mapping.upstream_token_id if mapping else None
        elif isinstance(token, AccessToken):
            lineage = token.claims.get("lineage")
        else:
            return
        if not lineage:
            return
        upstream = await self._upstream_token_store.get(key=lineage)
        if upstream is None or upstream.client_id != token.client_id:
            return
        # Deleting the shared lineage invalidates every generation immediately.
        # Stale JTI/refresh metadata expire naturally and can no longer resolve.
        await self._upstream_token_store.delete(key=lineage)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                await client.post(
                    f"{self.settings.matrix_api_url}/_matrix/client/v3/logout",
                    headers={"Authorization": f"Bearer {upstream.access_token}"},
                    json={},
                )
        except httpx.HTTPError:
            pass  # Local revocation remains effective even when Matrix is unavailable.
