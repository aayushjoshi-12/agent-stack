import asyncio
import os

from dotenv import load_dotenv
from langchain.tools import tool

from agent_stack.core.agent import AgentConfig
from agent_stack.core.factory import AgentFactory
from agent_stack.core.session import SessionStore

load_dotenv()


@tool
def ls_repo(repo_name: str) -> int:
    """ls command in the given repo name"""
    return os.listdir(path=f"/Users/aayushjoshi_12/repos/{repo_name}")


@tool
def list_repos():
    """see all the repos"""
    return os.listdir(path="/Users/aayushjoshi_12/repos")


agent_config = AgentConfig(
    name="test_agent",
    model="openrouter:z-ai/glm-5.3",
    tools=[ls_repo, list_repos],
    system_prompt="You are a helpful assistant that can add numbers.",
)


session_store = SessionStore()
factory = AgentFactory(event_bus=None, session_store=session_store, checkpoint_store=None)

test_agent = factory.create(agent_config)


async def run():
    response1 = await test_agent.run("can you list all the repos", session_id="test-123-123")
    response2 = await test_agent.run(
        "can you show all the files in agent-stack repo", session_id="test-123-123"
    )
    response3 = await test_agent.run("what was my first question", session_id="test-123-123")

    print(response1)
    print("==" * 50)
    print(response2)
    print("==" * 50)
    print(response3)


if __name__ == "__main__":
    asyncio.run(run())
