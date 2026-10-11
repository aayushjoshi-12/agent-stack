from langchain.tools import BaseTool


class ToolRegistry:
    def __init__(self, tools: tuple[BaseTool, ...]):
        self._tools = {}

        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")

            self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool: {name}")

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())
