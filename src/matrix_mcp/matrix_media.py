from __future__ import annotations

import base64
import binascii
import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel

from matrix_mcp.matrix_http import MatrixHTTPError, quote_matrix_id, quote_transaction_id

if TYPE_CHECKING:
    from matrix_mcp.matrix_http import MatrixHTTP

MAX_MEDIA_BYTES = 5 * 1024 * 1024
MAX_SAFE_JSON_INTEGER = 2**53 - 1
_SERVER_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_SERVER_NAME = rf"(?:{_SERVER_LABEL}\.)*{_SERVER_LABEL}\.?"
MXC_URI_PATTERN = (
    rf"^[Mm][Xx][Cc]://(?:{_SERVER_NAME}|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?/"
    r"[A-Za-z0-9_-]+$"
)
_MAX_ENCODED_LENGTH = 4 * ((MAX_MEDIA_BYTES + 2) // 3)
_CHUNK_BYTES = 64 * 1024


class UploadedMedia(BaseModel):
    content_uri: str
    filename: str
    content_type: str
    size: int


class DownloadedMedia(BaseModel):
    media_url: str
    content_type: str
    size: int
    data_base64: str


class MatrixMedia:
    def __init__(self, http: MatrixHTTP) -> None:
        self.http = http

    async def upload(
        self,
        data_base64: str,
        filename: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> UploadedMedia:
        _validate_filename(filename)
        _validate_mime(content_type)
        if len(data_base64) > _MAX_ENCODED_LENGTH:
            msg = "Media exceeds the 5 MiB size limit"
            raise ValueError(msg)
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError):
            msg = "Media must be valid base64"
            raise ValueError(msg) from None
        if base64.b64encode(data).decode("ascii") != data_base64:
            msg = "Media must use canonical base64 encoding"
            raise ValueError(msg)
        if len(data) > MAX_MEDIA_BYTES:
            msg = "Media exceeds the 5 MiB size limit"
            raise ValueError(msg)
        result = await self.http.json(
            "POST",
            "/_matrix/media/v3/upload",
            content=data,
            params={"filename": filename},
            headers={"Content-Type": content_type},
        )
        uri = result.get("content_uri")
        if not isinstance(uri, str):
            msg = "Matrix upload returned an invalid media URI"
            raise RuntimeError(msg)  # noqa: TRY004 - Invalid upstream response.
        _mxc_parts(uri)
        return UploadedMedia(
            content_uri=uri,
            filename=filename,
            content_type=content_type,
            size=len(data),
        )

    async def download(self, media_url: str) -> DownloadedMedia:
        server, media_id = _mxc_parts(media_url)
        route = (
            f"/_matrix/client/v1/media/download/{quote(server, safe='')}/{quote(media_id, safe='')}"
        )
        try:
            async with (
                self.http.client() as client,
                client.stream("GET", route, headers={"Accept-Encoding": "identity"}) as response,
            ):
                if response.is_redirect:
                    msg = f"Matrix media download refused HTTP redirect ({response.status_code})"
                    raise MatrixHTTPError(msg, status_code=response.status_code)
                data = await _read_media(response)
                if response.is_error:
                    _raise_download_error(response.status_code, data)
                content_type = response.headers.get("content-type", "application/octet-stream")
        except httpx.HTTPError as exc:
            msg = "Matrix media download failed before receiving complete content"
            raise RuntimeError(msg) from exc
        return DownloadedMedia(
            media_url=media_url,
            content_type=content_type,
            size=len(data),
            data_base64=base64.b64encode(data).decode("ascii"),
        )

    async def send(  # noqa: PLR0913 - Matrix attachment metadata.
        self,
        room_id: str,
        media_url: str,
        filename: str,
        *,
        content_type: str = "application/octet-stream",
        size: int | None = None,
        thread_id: str | None = None,
        transaction_id: str | None = None,
    ) -> str:
        room = quote_matrix_id(room_id, sigil="!", label="room ID")
        _mxc_parts(media_url)
        _validate_filename(filename)
        _validate_mime(content_type)
        if size is not None and (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_SAFE_JSON_INTEGER
        ):
            msg = f"Media size must be an integer between 0 and {MAX_SAFE_JSON_INTEGER}"
            raise ValueError(msg)
        if thread_id is not None:
            quote_matrix_id(thread_id, sigil="$", label="thread ID")
        transaction = quote_transaction_id(
            uuid4().hex if transaction_id is None else transaction_id
        )
        media_type = content_type.split("/", 1)[0].lower()
        msgtype = f"m.{media_type}" if media_type in {"image", "audio", "video"} else "m.file"
        info: dict[str, object] = {"mimetype": content_type}
        if size is not None:
            info["size"] = size
        content: dict[str, object] = {
            "msgtype": msgtype,
            "body": filename,
            "filename": filename,
            "url": media_url,
            "info": info,
        }
        if thread_id is not None:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
                "is_falling_back": False,
            }
        await self.http.require_unencrypted(room_id)
        result = await self.http.json(
            "PUT",
            f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{transaction}",
            body=content,
        )
        event_id = result.get("event_id")
        if not isinstance(event_id, str):
            msg = "Matrix media send returned an invalid event ID"
            raise RuntimeError(msg)  # noqa: TRY004 - Invalid upstream response.
        quote_matrix_id(event_id, sigil="$", label="event ID")
        return event_id


def _mxc_parts(uri: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(uri)
        valid = (
            re.fullmatch(MXC_URI_PATTERN, uri) is not None
            and parsed.scheme == "mxc"
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        msg = "media_url must be a valid mxc://server/media_id URI"
        raise ValueError(msg)
    return parsed.netloc, parsed.path[1:]


def _validate_filename(filename: str) -> None:
    if (
        not filename.strip()
        or filename in {".", ".."}
        or re.search(r"[\x00-\x1f\x7f/\\]", filename) is not None
    ):
        msg = "filename must be a nonempty file name without a path or control characters"
        raise ValueError(msg)


def _validate_mime(content_type: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type) is None:
        msg = "content_type must be a MIME type/subtype without parameters"
        raise ValueError(msg)


async def _read_media(response: httpx.Response) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        msg = "Matrix media Content-Encoding must be identity to enforce the byte limit"
        raise ValueError(msg)
    length = response.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            declared = 0
        if declared > MAX_MEDIA_BYTES:
            msg = "Media exceeds the 5 MiB size limit"
            raise ValueError(msg)
    content = bytearray()
    async for chunk in response.aiter_raw(chunk_size=_CHUNK_BYTES):
        if len(content) + len(chunk) > MAX_MEDIA_BYTES:
            msg = "Media exceeds the 5 MiB size limit"
            raise ValueError(msg)
        content.extend(chunk)
    return bytes(content)


def _raise_download_error(status: int, content: bytes) -> None:
    errcode = None
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        value = payload.get("errcode")
        if isinstance(value, str) and re.fullmatch(r"M_[A-Z_0-9]{1,80}", value):
            errcode = value
    detail = f" ({errcode})" if errcode else ""
    msg = f"Matrix media download failed with HTTP {status}{detail}"
    raise MatrixHTTPError(msg, status_code=status, errcode=errcode)
