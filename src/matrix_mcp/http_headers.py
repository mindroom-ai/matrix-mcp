from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HTTPHeaderConfig:
    headers: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)

    def resolve(self) -> dict[str, str]:
        return resolve_http_headers(self.headers, self.commands)


def parse_http_headers(header_values: list[str] | None) -> dict[str, str]:
    return _parse_header_mapping(header_values, value_label="value")


def parse_http_header_commands(header_command_values: list[str] | None) -> dict[str, str]:
    return _parse_header_mapping(header_command_values, value_label="command")


def resolve_http_headers(
    http_headers: dict[str, str] | None = None,
    http_header_commands: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = dict(http_headers or {})
    for name, command in (http_header_commands or {}).items():
        headers[name] = _run_header_command(name=name, command=command)
    return headers


def _parse_header_mapping(header_values: list[str] | None, *, value_label: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_header in header_values or []:
        name, separator, value = raw_header.partition(":")
        if not separator or not name.strip() or not value.strip():
            msg = f"Invalid header {raw_header!r}; expected 'Name: {value_label}'"
            raise ValueError(msg)
        headers[name.strip()] = value.strip()
    return headers


def _run_header_command(*, name: str, command: str) -> str:
    if _is_cloudflare_access_header_command(name=name, command=command):
        _require_cloudflared("The stored cf-access-token header command")

    try:
        args = shlex.split(command)
    except ValueError as exc:
        msg = f"Invalid command for HTTP header {name!r}: {exc}"
        raise RuntimeError(msg) from exc
    if not args:
        msg = f"HTTP header command for {name!r} is empty"
        raise RuntimeError(msg)

    try:
        completed = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        msg = f"Failed to run HTTP header command for {name!r}: {exc}"
        raise RuntimeError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"HTTP header command for {name!r} timed out"
        raise RuntimeError(msg) from exc

    if completed.returncode != 0:
        detail = (
            completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        )
        msg = f"HTTP header command for {name!r} failed: {detail}"
        raise RuntimeError(msg)

    value = completed.stdout.strip()
    if not value:
        msg = f"HTTP header command for {name!r} produced no output"
        raise RuntimeError(msg)
    return value


def cloudflared_missing_message(context: str) -> str:
    return (
        f"{context} requires the cloudflared CLI, but it was not found on PATH.\n\n"
        "Install cloudflared, then rerun this command.\n\n"
        "macOS with Homebrew:\n"
        "  brew install cloudflared\n\n"
        "Other platforms:\n"
        "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    )


def _require_cloudflared(context: str) -> None:
    if shutil.which("cloudflared") is None:
        raise RuntimeError(cloudflared_missing_message(context))


def _is_cloudflare_access_header_command(*, name: str, command: str) -> bool:
    return name.lower() == "cf-access-token" and "cloudflared access" in command
