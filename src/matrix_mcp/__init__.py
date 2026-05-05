"""Matrix MCP package."""

__all__ = ["__version__"]

try:
    from matrix_mcp._version import __version__
except ImportError:
    __version__ = "0.0.0"
