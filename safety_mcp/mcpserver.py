"""FastMCP с проверкой лишних параметров: библиотека молча отбрасывает неизвестные аргументы,
а агент должен узнать, что его параметр не применился."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


class StrictFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tool = self._tool_manager.get_tool(name)
        if tool is not None and isinstance(arguments, dict):
            allowed = set((tool.parameters or {}).get("properties", {}))
            extra = sorted(set(arguments) - allowed)
            if extra:
                raise ToolError(f"ОШИБКА: неизвестные параметры {extra}; допустимые: {sorted(allowed) or 'нет'}. "
                                "Команда не выполнена.")
        return await super().call_tool(name, arguments)
