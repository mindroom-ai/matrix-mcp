from __future__ import annotations

import asyncio
import base64
import gzip
import json
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from matrix_mcp.config import MatrixMCPConfig
from matrix_mcp.matrix_http import MatrixHTTP
from matrix_mcp.matrix_media import MatrixMedia

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ROOM = "!room:example.com"
DOWNLOAD = "/_matrix/client/v1/media/download/example.com/file"


@dataclass
class MediaEndpoint:
    requests: list[dict[str, Any]] = field(default_factory=list)
    download: bytes = b"hello\n"
    download_status: int = 200
    download_headers: dict[str, str] = field(default_factory=dict)
    encrypted: bool = False
    chunked: bool = False
    queued_downloads: deque[tuple[bytes, int, dict[str, str]]] = field(default_factory=deque)

    async def handle(  # noqa: PLR0911 - Fake HTTP endpoint dispatch.
        self,
        request: web.Request,
    ) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.path == "/_matrix/media/v3/upload":
            return web.json_response({"content_uri": "mxc://example.com/file"})
        if request.path == DOWNLOAD:
            if self.queued_downloads:
                body, status, headers = self.queued_downloads.popleft()
                return web.Response(body=body, status=status, headers=headers)
            if self.chunked:
                stream = web.StreamResponse(headers={"Content-Type": "text/plain"})
                await stream.prepare(request)
                try:
                    for offset in range(0, len(self.download), 64 * 1024):
                        await stream.write(self.download[offset : offset + 64 * 1024])
                    await stream.write_eof()
                except ConnectionResetError:
                    pass  # Reader deliberately stops once its byte bound is exceeded.
                return stream
            return web.Response(
                body=self.download,
                status=self.download_status,
                headers=self.download_headers,
                content_type="text/plain",
            )
        if request.path.endswith("/state/m.room.encryption"):
            if self.encrypted:
                return web.json_response({"algorithm": "m.megolm.v1.aes-sha2"})
            return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)
        if "/send/m.room.message/" in request.path:
            return web.json_response({"event_id": "$file"})
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)


@pytest.fixture
async def matrix() -> AsyncIterator[tuple[MatrixMedia, MediaEndpoint]]:
    endpoint = MediaEndpoint()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", endpoint.handle)
    async with TestServer(app) as server:
        http = MatrixHTTP(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/")),
                user_id="@alice:example.com",
                device_id="TESTDEVICE",
                access_token="test-token",
                http_headers={"X-Example": "custom-value"},
            )
        )
        yield MatrixMedia(http), endpoint


async def test_upload_preserves_bytes_metadata_and_auth(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    result = await media.upload("aGVsbG8K", "hello.txt", content_type="text/plain")
    assert result.content_uri == "mxc://example.com/file"
    assert result.size == 6
    assert result.filename == "hello.txt"
    request = endpoint.requests[-1]
    assert request["body"] == b"hello\n"
    assert request["query"]["filename"] == "hello.txt"
    assert request["headers"]["Authorization"] == "Bearer test-token"
    assert request["headers"]["X-Example"] == "custom-value"
    assert request["headers"]["Content-Type"] == "text/plain"


async def test_download_uses_authenticated_media_endpoint(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    result = await media.download("mxc://example.com/file")
    assert base64.b64decode(result.data_base64) == b"hello\n"
    assert result.size == 6
    assert result.content_type.startswith("text/plain")
    assert endpoint.requests[-1]["path"] == DOWNLOAD
    assert endpoint.requests[-1]["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("scheme", ["MXC", "MxC"])
async def test_download_accepts_case_insensitive_mxc_scheme(
    matrix: tuple[MatrixMedia, MediaEndpoint], scheme: str
) -> None:
    media, endpoint = matrix
    media_url = f"{scheme}://example.com/file"

    result = await media.download(media_url)

    assert result.media_url == media_url
    assert endpoint.requests[-1]["path"] == DOWNLOAD


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/file",
        "file:///etc/passwd",
        "mxc://example.com/../secret",
        "mxc://example..com/file",
        "mxc://./file",
        "mxc://-example.com/file",
        "mxc://example-.com/file",
        "mxc://éxample.com/file",
    ],
)
async def test_download_refuses_non_matrix_and_invalid_media_uris(
    matrix: tuple[MatrixMedia, MediaEndpoint], uri: str
) -> None:
    media, endpoint = matrix
    with pytest.raises(ValueError, match=r"mxc|media"):
        await media.download(uri)
    assert not endpoint.requests


@pytest.mark.parametrize("encoded", ["%%%", "aGVsbG8K\n", "AAAA="])
async def test_upload_rejects_noncanonical_base64_before_http(
    matrix: tuple[MatrixMedia, MediaEndpoint], encoded: str
) -> None:
    media, endpoint = matrix
    with pytest.raises(ValueError, match="base64"):
        await media.upload(encoded, "file.bin")
    assert not endpoint.requests


async def test_upload_rejects_oversized_payload_before_http(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    encoded = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
    with pytest.raises(ValueError, match=r"limit|MiB|size"):
        await media.upload(encoded, "file.bin")
    assert not endpoint.requests


async def test_download_refuses_redirect_without_following_it(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download_status = 302
    endpoint.download_headers = {"Location": "https://example.com/other"}
    with pytest.raises(RuntimeError, match=r"redirect|302"):
        await media.download("mxc://example.com/file")
    assert len(endpoint.requests) == 1


async def test_encrypted_room_never_receives_media_event(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.encrypted = True
    with pytest.raises(RuntimeError, match="encrypted"):
        await media.send(ROOM, "mxc://example.com/file", "hello.txt")
    assert all("/send/" not in request["path"] for request in endpoint.requests)


@pytest.mark.parametrize(
    ("mime", "msgtype"),
    [
        ("image/png", "m.image"),
        ("audio/ogg", "m.audio"),
        ("video/mp4", "m.video"),
        ("IMAGE/PNG", "m.image"),
        ("text/plain", "m.file"),
    ],
)
async def test_send_media_preserves_type_size_thread_and_transaction(
    matrix: tuple[MatrixMedia, MediaEndpoint],
    mime: str,
    msgtype: str,
) -> None:
    media, endpoint = matrix
    event_id = await media.send(
        ROOM,
        "mxc://example.com/file",
        "attachment",
        content_type=mime,
        size=6,
        thread_id="$thread",
        transaction_id="attachment-1",
    )
    assert event_id == "$file"
    request = endpoint.requests[-1]
    assert request["path"].endswith("/send/m.room.message/attachment-1")
    assert json.loads(request["body"]) == {
        "msgtype": msgtype,
        "body": "attachment",
        "filename": "attachment",
        "url": "mxc://example.com/file",
        "info": {"mimetype": mime, "size": 6},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread", "is_falling_back": False},
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"filename": "../secret"},
        {"content_type": "text/plain\r\nAuthorization: injected"},
        {"transaction_id": ""},
        {"size": -1},
        {"size": 2**53},
        {"thread_id": "thread"},
    ],
)
async def test_invalid_attachment_metadata_fails_before_http(
    matrix: tuple[MatrixMedia, MediaEndpoint],
    arguments: dict[str, Any],
) -> None:
    media, endpoint = matrix
    kwargs: dict[str, Any] = {"filename": "file.bin", **arguments}
    with pytest.raises(ValueError, match=r"filename|content_type|transaction|size|thread"):
        await media.send(ROOM, "mxc://example.com/file", **kwargs)
    assert not endpoint.requests


async def test_download_rejects_oversized_content_length(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download = b"x" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="5 MiB"):
        await media.download("mxc://example.com/file")


async def test_download_requests_identity_encoding(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    await media.download("mxc://example.com/file")
    assert endpoint.requests[-1]["headers"]["Accept-Encoding"] == "identity"


async def test_download_failure_does_not_echo_server_error_text(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download_status = 403
    endpoint.download = json.dumps({"errcode": "M_FORBIDDEN", "error": "do-not-echo-this"}).encode()
    with pytest.raises(RuntimeError, match="M_FORBIDDEN") as error:
        await media.download("mxc://example.com/file")
    assert "do-not-echo-this" not in str(error.value)


async def test_download_preserves_deep_json_file_bytes(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    deep_json = b"[" * 100_000 + b"0" + b"]" * 100_000
    endpoint.download = deep_json

    result = await media.download("mxc://example.com/file")

    assert base64.b64decode(result.data_base64) == deep_json


async def test_download_sanitizes_recursive_error_json(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download_status = 500
    endpoint.download = b"[" * 100_000 + b"0" + b"]" * 100_000

    with pytest.raises(RuntimeError, match=r"HTTP 500") as error:
        await media.download("mxc://example.com/file")

    assert "M_FORBIDDEN" not in str(error.value)


async def test_download_sanitizes_malformed_error_body(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download_status = 500
    endpoint.download = b'\xff{"errcode":"M_FORBIDDEN"}'

    with pytest.raises(RuntimeError, match=r"HTTP 500") as error:
        await media.download("mxc://example.com/file")

    assert "M_FORBIDDEN" not in str(error.value)


async def test_download_retries_a_rate_limited_response(
    matrix: tuple[MatrixMedia, MediaEndpoint], monkeypatch: pytest.MonkeyPatch
) -> None:
    media, endpoint = matrix
    endpoint.queued_downloads.extend(
        [
            (
                json.dumps({"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 1}).encode(),
                429,
                {"Content-Type": "application/json"},
            ),
            (b"retried\n", 200, {"Content-Type": "text/plain"}),
        ]
    )
    delays: list[float] = []

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_delay)

    result = await media.download("mxc://example.com/file")

    assert base64.b64decode(result.data_base64) == b"retried\n"
    assert [request["path"] for request in endpoint.requests] == [DOWNLOAD, DOWNLOAD]
    assert delays == [0.001]


async def test_download_enforces_byte_limit_without_content_length(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.chunked = True
    endpoint.download = b"x" * (5 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="5 MiB"):
        await media.download("mxc://example.com/file")


async def test_download_refuses_http_compression_before_decoding(
    matrix: tuple[MatrixMedia, MediaEndpoint],
) -> None:
    media, endpoint = matrix
    endpoint.download = gzip.compress(b"hello")
    endpoint.download_headers = {"Content-Encoding": "gzip"}
    with pytest.raises(ValueError, match="Content-Encoding"):
        await media.download("mxc://example.com/file")


async def test_download_preserves_configured_homeserver_path_prefix() -> None:
    requests: list[str] = []
    endpoint_path = f"/prefix{DOWNLOAD}"

    async def handle(request: web.Request) -> web.Response:
        requests.append(request.path)
        if request.path == endpoint_path:
            return web.Response(body=b"hello\n", content_type="text/plain")
        return web.json_response({"errcode": "M_NOT_FOUND"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    async with TestServer(app) as server:
        http = MatrixHTTP(
            MatrixMCPConfig(
                homeserver=str(server.make_url("/prefix")),
                user_id="@alice:example.com",
                device_id="TESTDEVICE",
                access_token="test-token",
            )
        )
        result = await MatrixMedia(http).download("mxc://example.com/file")

    assert base64.b64decode(result.data_base64) == b"hello\n"
    assert requests == [endpoint_path]
