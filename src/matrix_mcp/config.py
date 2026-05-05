from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

APP_NAME = "matrix-mcp"


def default_config_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "config.json"


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    homeserver: str
    user_id: str | None = None
    device_id: str | None = None
    access_token: str | None = Field(default=None, repr=False)

    def access_token_value(self) -> str | None:
        return self.access_token

    def model_dump_safe(self) -> dict[str, Any]:
        dumped = self.model_dump(mode="json")
        if self.access_token_value():
            dumped["access_token"] = "<configured>"
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
