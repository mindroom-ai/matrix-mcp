from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_e2ee_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep encryption stores out of the real user config directory."""
    root: Path = tmp_path_factory.mktemp("config")
    monkeypatch.setattr("matrix_mcp.e2ee.default_config_path", lambda: root / "config.json")
