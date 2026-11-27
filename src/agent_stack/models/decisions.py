"""Decision models: one call answers a multiple-choice question about a piece of text.

OpenRouter serves them (TypeSafe's Jev, ``~typesafe/jev-latest``) on ``/api/alpha/decisions``,
not on chat completions, so they are not ``BaseChatModel``s. Auto mode asks one which verdict
fits a tool call: it answers in about half a second for a few thousandths of a cent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from agent_stack.core.errors import ConfigurationError

__all__ = ["PREFIX", "Choice", "DecisionModel", "build_decision_model"]

_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
#: The prefix that names a decision model in ``AGENT_STACK_AUTO_MODEL``.
PREFIX = "decisions:"


@dataclass(frozen=True)
class Choice:
    """The option picked, how sure the model is (0 to 1), and what the call cost in USD."""

    choice: str | None
    confidence: float
    cost: float


class DecisionModel:
    """A decision model on OpenRouter, asked one ``choice`` question per call."""

    def __init__(self, model: str, api_key: str, *, timeout: float = 20.0) -> None:
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout

    async def choose(self, state: str, instructions: str, options: Mapping[str, str]) -> Choice:
        """Pick one of ``options`` (key to description) for ``state``.

        A refusal, a missing answer or an unknown key comes back as ``choice=None``: the
        caller treats it as no answer, never as one of the options. Raises on HTTP errors.
        """
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                "pick": {"type": "choice", "instructions": instructions, "criteria": dict(options)}
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(_ENDPOINT, headers=self._headers, json=body)
        response.raise_for_status()
        data = response.json()
        answer = (data.get("answers") or {}).get("pick") or {}
        cost = float((data.get("usage") or {}).get("cost") or 0.0)
        picked = answer.get("choice") if answer.get("type") == "choice" else None
        if picked not in options:
            return Choice(None, 0.0, cost)
        return Choice(str(picked), float(answer.get("confidence") or 0.0), cost)


def build_decision_model(name: str, api_key: str | None) -> DecisionModel:
    """``decisions:<openrouter model id>`` to a client; it needs an OpenRouter key."""
    if not api_key:
        raise ConfigurationError("a decision model needs OPENROUTER_API_KEY")
    return DecisionModel(name.removeprefix(PREFIX), api_key)
