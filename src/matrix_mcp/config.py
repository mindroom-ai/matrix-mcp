from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

APP_NAME = "matrix-mcp"


def default_config_path() -> Path:
    candidates = _default_config_path_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _default_config_path_candidates() -> tuple[Path, ...]:
    platform_path = Path(user_config_dir(APP_NAME, appauthor=False)) / "config.json"
    home_dot_config_path = Path.home() / ".config" / APP_NAME / "config.json"
    if home_dot_config_path == platform_path:
        return (platform_path,)
    return (platform_path, home_dot_config_path)


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    homeserver: str
    user_id: str | None = None
    device_id: str | None = None
    access_token: str | None = Field(default=None, repr=False)
    http_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    http_header_commands: dict[str, str] = Field(default_factory=dict, repr=False)

    def access_token_value(self) -> str | None:
        return self.access_token

    def model_dump_safe(self) -> dict[str, Any]:
        dumped = self.model_dump(mode="json")
        if self.access_token_value():
            dumped["access_token"] = "<configured>"
        if self.http_headers:
            dumped["http_headers"] = dict.fromkeys(self.http_headers, "<configured>")
        if self.http_header_commands:
            dumped["http_header_commands"] = dict.fromkeys(
                self.http_header_commands,
                "<configured>",
            )
        return dumped


class MatrixMCPConfig(AuthConfig):
    default_limit: int = 20

    @classmethod
    def load(cls, path: Path | None = None) -> MatrixMCPConfig:
        resolved_path = path or default_config_path()
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path | None = None) -> None:
        resolved_path = path or default_config_path()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @property
    def normalized_homeserver(self) -> str:
        return str(AnyHttpUrl(self.homeserver)).rstrip("/")
