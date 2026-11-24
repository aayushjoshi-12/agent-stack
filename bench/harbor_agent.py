"""Harbor adapter: run agent-stack inside a Terminal-Bench task container.

    harbor run -d terminal-bench@2.0 --agent-import-path bench.harbor_agent:AgentStackAgent \
        -m openrouter:z-ai/glm-5.3-flash -n 1

With AGENT_STACK_PREBUILT=<tarball from bench/prebuild.sh> the environment is simply unpacked;
otherwise the source is packed from git HEAD, uploaded to /opt/agent-stack and installed with uv
(about nine minutes under emulation, so prebuild). The agent
runs headless (``agent-stack run --yes -``): the container is the sandbox, so every tool is
approved. The model key is read from the *Harbor process* environment (OPENROUTER_API_KEY) and
handed to the container only for the agent command. Grading is Harbor's, never the agent's.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_REPO = Path(__file__).resolve().parents[1]
_REMOTE = "/opt/agent-stack"


class AgentStackAgent(BaseInstalledAgent):
    @staticmethod
    @override
    def name() -> str:
        return "agent-stack"

    @override
    def get_version_command(self) -> str | None:
        return f"{_REMOTE}/.venv/bin/agent-stack --help | head -1"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        prebuilt = os.environ.get("AGENT_STACK_PREBUILT")
        if prebuilt:  # built once by bench/prebuild.sh; seconds instead of ~9 minutes emulated
            await environment.upload_file(Path(prebuilt), "/tmp/env.tar.gz")
            await self.exec_as_root(
                environment,
                command="tar -C /opt -xzf /tmp/env.tar.gz && rm /tmp/env.tar.gz",
            )
            return
        await self.ensure_system_dependencies(environment, ("curl", "git"))
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "agent-stack.tar"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(_REPO),
                    "archive",
                    "-o",
                    str(archive),
                    "HEAD",
                    "src",
                    "pyproject.toml",
                    "uv.lock",
                    "README.md",
                ],
                check=True,
            )
            await environment.upload_file(archive, "/tmp/agent-stack.tar")
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; mkdir -p /opt/agent-stack && "
                "tar -xf /tmp/agent-stack.tar -C /opt/agent-stack && "
                "curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && "
                "cd /opt/agent-stack && uv sync --frozen --no-dev --python 3.12 && "
                "chmod -R a+rX /opt/agent-stack"
            ),
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        pass

    @override
    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        env = {
            "AGENT_STACK_MODEL": self.model_name or os.environ["AGENT_STACK_MODEL"],
            "OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"],
            "AGENT_STACK_APPROVAL_TOOLS": "",
            # no per-trial caps: the step limit is effectively off, the context budget is the
            # default; total spend is bounded by bench/spend_guard.py instead
            "AGENT_STACK_MAX_STEPS": os.environ.get("AGENT_STACK_MAX_STEPS", "500"),
            "AGENT_STACK_DB": "/tmp/agent-stack.db",
            "AGENT_STACK_HOME": "/tmp/agent-stack-home",
        }
        await self.exec_as_agent(
            environment,
            env=env,
            command=(
                'export AGENT_STACK_WORKSPACE="$PWD"; '
                f"printf %s {shlex.quote(instruction)} | "
                f"{_REMOTE}/.venv/bin/agent-stack run --yes - "
                "2>&1 | tee /logs/agent/agent-stack.txt"
            ),
        )
