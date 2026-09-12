from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from matrix_mcp.http_headers import resolve_http_headers
from matrix_mcp.tls import default_ssl_context

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from matrix_mcp.config import MatrixMCPConfig


_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_RATE_LIMIT_DELAY_SECONDS = 5.0
_RATE_LIMIT_RETRIES = 2
_CONTROL_CODE_BOUNDARY = 32
_MAX_TRANSACTION_ID_LENGTH = 255
_ERRCODE_PATTERN = re.compile(r"M_[A-Z_0-9]{1,80}")


class MatrixHTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, errcode: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errcode = errcode


class MatrixHTTP:
    def __init__(self, config: MatrixMCPConfig) -> None:
        token = config.access_token_value()
        if not token or not config.user_id:
            msg = "Matrix credentials are incomplete. Run `matrix-mcp auth` first."
            raise RuntimeError(msg)
        self._config = config
        self._access_token = token
        self._homeserver = config.normalized_homeserver
        self.user_id = config.user_id

    def client(self) -> AbstractAsyncContextManager[httpx.AsyncClient]:
        try:
            headers = resolve_http_headers(
                self._config.http_headers,
                self._config.http_header_commands,
            )
        except (RuntimeError, ValueError):
            msg = "Matrix custom HTTP header resolution failed"
            raise RuntimeError(msg) from None
        headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self._access_token}",
            }
        )
        return httpx.AsyncClient(
            base_url=f"{self._homeserver}/",
            headers=headers,
            timeout=30,
            follow_redirects=False,
            verify=default_ssl_context(),
        )

    async def json(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            msg = "Matrix API path must start with '/'"
            raise ValueError(msg)
        if body is not None and content is not None:
            msg = "Matrix API request cannot include both body and content"
            raise ValueError(msg)
        if headers is not None and any(name.lower() == "authorization" for name in headers):
            msg = "Matrix API request headers cannot override authorization"
            raise ValueError(msg)
        request_headers = dict(headers or {})
        request_headers["Accept-Encoding"] = "identity"
        url = f"{self._homeserver}{path}"
        async with self.client() as client:
            for attempt in range(_RATE_LIMIT_RETRIES + 1):
                try:
                    async with client.stream(
                        method,
                        url,
                        json=body,
                        content=content,
                        headers=request_headers,
                        params=params,
                    ) as response:
                        if response.is_redirect:
                            msg = (
                                f"Matrix API request refused HTTP redirect ({response.status_code})"
                            )
                            raise MatrixHTTPError(msg, status_code=response.status_code)
                        response_content = await _read_bounded(response)
                except httpx.HTTPError as exc:
                    msg = "Matrix API request failed before receiving a response"
                    raise RuntimeError(msg) from exc

                payload = _response_payload(response, response_content)
                errcode = _safe_errcode(payload.get("errcode"))
                if (
                    response.status_code == HTTPStatus.TOO_MANY_REQUESTS
                    and attempt < _RATE_LIMIT_RETRIES
                ):
                    delay = _retry_delay(response, payload)
                    if delay > _MAX_RATE_LIMIT_DELAY_SECONDS:
                        msg = "Matrix API rate limit requires waiting more than 5 seconds"
                        raise MatrixHTTPError(
                            msg,
                            status_code=response.status_code,
                            errcode=errcode,
                        )
                    await asyncio.sleep(delay)
                    continue
                if response.is_error:
                    detail = f" ({errcode})" if errcode else ""
                    msg = f"Matrix API request failed with HTTP {response.status_code}{detail}"
                    raise MatrixHTTPError(
                        msg,
                        status_code=response.status_code,
                        errcode=errcode,
                    )
                return payload
        msg = "Matrix API request exhausted its retry budget"
        raise RuntimeError(msg)

    async def require_unencrypted(self, room_id: str) -> None:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        path = f"/_matrix/client/v3/rooms/{room}/state/m.room.encryption"
        try:
            await self.json("GET", path)
        except MatrixHTTPError as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND and exc.errcode == "M_NOT_FOUND":
                return
            raise
        msg = "Plaintext sends are not supported in encrypted Matrix rooms"
        raise RuntimeError(msg)


def quote_matrix_id(value: str, *, sigil: str, label: str) -> str:
    if (
        not value.startswith(sigil)
        or len(value) == 1
        or any(char.isspace() or ord(char) < _CONTROL_CODE_BOUNDARY for char in value)
    ):
        msg = f"Invalid Matrix {label}"
        raise ValueError(msg)
    return quote(value, safe="")


def quote_transaction_id(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or len(value) > _MAX_TRANSACTION_ID_LENGTH
        or any(char.isspace() or ord(char) < _CONTROL_CODE_BOUNDARY for char in value)
    ):
        msg = "Invalid Matrix transaction ID"
        raise ValueError(msg)
    return quote(value, safe="")


async def _read_bounded(response: httpx.Response) -> bytes:
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        msg = "Matrix API response used an unsupported content encoding"
        raise RuntimeError(msg)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_JSON_BYTES:
                msg = "Matrix API response exceeded the JSON size limit"
                raise RuntimeError(msg)
        except ValueError:
            pass
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > _MAX_JSON_BYTES:
            msg = "Matrix API response exceeded the JSON size limit"
            raise RuntimeError(msg)
    return bytes(content)


def _json_object(content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "Matrix API returned an invalid JSON object"
        raise RuntimeError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Matrix API returned an invalid JSON object"
        raise RuntimeError(msg)  # noqa: TRY004
    return payload


def _response_payload(response: httpx.Response, content: bytes) -> dict[str, Any]:
    try:
        return _json_object(content)
    except RuntimeError as exc:
        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            return {}
        if response.is_error:
            msg = f"Matrix API request failed with HTTP {response.status_code}"
            raise MatrixHTTPError(msg, status_code=response.status_code) from exc
        raise


def _retry_delay(response: httpx.Response, payload: dict[str, Any]) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    retry_after_ms = payload.get("retry_after_ms")
    if isinstance(retry_after_ms, int) and not isinstance(retry_after_ms, bool):
        return max(0.0, retry_after_ms / 1000)
    return 0.0


def _safe_errcode(value: object) -> str | None:
    return value if isinstance(value, str) and _ERRCODE_PATTERN.fullmatch(value) else None
