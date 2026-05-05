from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from matrix_mcp.config import MatrixMCPConfig, default_config_path

if TYPE_CHECKING:
    from pathlib import Path


class MatrixIdBucket(BaseModel):
    counter: int = 0
    id_to_matrix: dict[int, str] = Field(default_factory=dict)
    matrix_to_id: dict[str, int] = Field(default_factory=dict)

    @field_validator("id_to_matrix", mode="before")
    @classmethod
    def _convert_id_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            return {int(key) if isinstance(key, str) else key: item for key, item in value.items()}
        return value

    def get_or_create(self, matrix_id: str) -> int:
        if matrix_id in self.matrix_to_id:
            return self.matrix_to_id[matrix_id]
        self.counter += 1
        simple_id = self.counter
        self.id_to_matrix[simple_id] = matrix_id
        self.matrix_to_id[matrix_id] = simple_id
        return simple_id

    def resolve(self, simple_id: int) -> str | None:
        return self.id_to_matrix.get(simple_id)


class MatrixIdState(BaseModel):
    rooms: MatrixIdBucket = Field(default_factory=MatrixIdBucket)
    events: MatrixIdBucket = Field(default_factory=MatrixIdBucket)


class MatrixIdStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._state = self._load()

    @classmethod
    def for_config(cls, config: MatrixMCPConfig) -> MatrixIdStore:
        return cls(id_store_path(config))

    def room_ref(self, room_id: str) -> int:
        room_ref = self._state.rooms.get_or_create(room_id)
        self.save()
        return room_ref

    def event_ref(self, event_id: str) -> int:
        event_ref = self._state.events.get_or_create(event_id)
        self.save()
        return event_ref

    def resolve_room(self, room_id_or_ref: str | int) -> str:
        return self._resolve(self._state.rooms, room_id_or_ref, kind="room")

    def resolve_event(self, event_id_or_ref: str | int) -> str:
        return self._resolve(self._state.events, event_id_or_ref, kind="event")

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(self._state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def _load(self) -> MatrixIdState:
        if not self._path.exists():
            return MatrixIdState()
        return MatrixIdState.model_validate(json.loads(self._path.read_text(encoding="utf-8")))

    @staticmethod
    def _resolve(bucket: MatrixIdBucket, value: str | int, *, kind: str) -> str:
        if isinstance(value, int):
            matrix_id = bucket.resolve(value)
            if matrix_id is not None:
                return matrix_id
            msg = f"Unknown Matrix {kind} ref {value!r}. Read or list it first."
            raise ValueError(msg)
        if value.isdigit():
            matrix_id = bucket.resolve(int(value))
            if matrix_id is not None:
                return matrix_id
            msg = f"Unknown Matrix {kind} ref {value!r}. Read or list it first."
            raise ValueError(msg)
        return value


def id_store_path(config: MatrixMCPConfig) -> Path:
    key = f"{config.normalized_homeserver}|{config.user_id or ''}".encode()
    digest = hashlib.sha256(key).hexdigest()[:16]
    return default_config_path().with_name(f"ids-{digest}.json")
