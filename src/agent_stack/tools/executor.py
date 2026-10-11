import asyncio

from langchain.messages import ToolMessage

from agent_stack.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def execute(self, tool_call) -> ToolMessage:
        tool = self.registry.get(tool_call["name"])
        result = await tool.ainvoke(tool_call)

        return ToolMessage(content=str(result), tool_call_id=tool_call["id"])

    async def execute_many(self, tool_calls) -> list[ToolMessage]:
        return await asyncio.gather(*[self.execute(tool_call) for tool_call in tool_calls])
