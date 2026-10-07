"""Absolute-path entry point for local MCP clients; no global installation."""

from netease_bridge.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
