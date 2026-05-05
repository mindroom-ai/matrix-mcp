from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from matrix_mcp.id_state import MatrixIdStore

if TYPE_CHECKING:
    from pathlib import Path


def test_id_store_assigns_stable_room_and_event_refs(tmp_path: Path) -> None:
    path = tmp_path / "ids.json"
    store = MatrixIdStore(path)

    assert store.room_ref("!room:example.com") == 1
    assert store.room_ref("!room:example.com") == 1
    assert store.event_ref("$event") == 1

    reloaded = MatrixIdStore(path)

    assert reloaded.resolve_room(1) == "!room:example.com"
    assert reloaded.resolve_room("1") == "!room:example.com"
    assert reloaded.resolve_event(1) == "$event"
    assert reloaded.resolve_event("$raw") == "$raw"


def test_id_store_rejects_unknown_numeric_refs(tmp_path: Path) -> None:
    store = MatrixIdStore(tmp_path / "ids.json")

    with pytest.raises(ValueError, match="Unknown Matrix room ref"):
        store.resolve_room(99)

    with pytest.raises(ValueError, match="Unknown Matrix event ref"):
        store.resolve_event("99")
