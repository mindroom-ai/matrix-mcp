from __future__ import annotations

import matrix_mcp


def test_package_exports_version() -> None:
    assert isinstance(matrix_mcp.__version__, str)
    assert matrix_mcp.__version__
