"""P40: the web API's rate limits: 429 with Retry-After, recovery, and an unlimited event stream."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from agent_stack.app.application import Application
from agent_stack.gateways.web import WebSettings, create_app
from tests.fakes import FakeChatModel, ai
from tests.integration.test_web_gateway import HEADERS, TOKEN, _application

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _limited(
    tmp_path: Path, clock: Clock, *, messages: int | None, requests: int | None
) -> AsyncIterator[httpx.AsyncClient]:
    application: Application = _application(
        tmp_path, FakeChatModel([ai("one"), ai("two"), ai("three")])
    )
    application.config = dataclasses.replace(
        application.config, rate_messages=messages, rate_requests=requests
    )
    app = create_app(application, WebSettings(token=TOKEN), clock=clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_a_burst_over_the_limit_gets_429_with_retry_after_then_recovers(
    tmp_path: Path,
) -> None:
    clock = Clock()
    async for client in _limited(tmp_path, clock, messages=10, requests=3):
        codes = [(await client.get("/sessions", headers=HEADERS)).status_code for _ in range(3)]
        assert codes == [200, 200, 200]
        refused = await client.get("/sessions", headers=HEADERS)
        assert refused.status_code == 429
        assert refused.headers["Retry-After"] == "20"  # 3 a minute: one back every 20 s
        assert "try again in 20s" in refused.json()["detail"]
        assert (await client.get("/health")).status_code == 200  # health is never limited
        # a wrong token is refused before the limiter, and does not spend the owner's tokens
        assert (await client.get("/sessions")).status_code == 401
        clock.now += 20
        assert (await client.get("/sessions", headers=HEADERS)).status_code == 200
        assert (await client.get("/sessions", headers=HEADERS)).status_code == 429


async def test_model_spending_calls_have_their_own_stricter_bucket_and_sse_is_free(
    tmp_path: Path,
) -> None:
    clock = Clock()
    async for client in _limited(tmp_path, clock, messages=1, requests=100):
        session = (await client.post("/sessions", headers=HEADERS)).json()["id"]
        sent = await client.post(
            f"/sessions/{session}/messages", headers=HEADERS, json={"text": "hi"}
        )
        assert sent.status_code == 202
        run_id = sent.json()["run_id"]
        again = await client.post(
            f"/sessions/{session}/messages", headers=HEADERS, json={"text": "again"}
        )
        assert again.status_code == 429 and again.headers["Retry-After"] == "60"
        for path, body in (
            (f"/runs/{run_id}/input", {"text": "more"}),
            (f"/sessions/{session}/compact", {}),
            (f"/sessions/{session}/branch", {"event_id": "x"}),
            (
                f"/sessions/{session}/rewind",
                {"owner_session_id": session, "started_event_id": "x"},
            ),
        ):
            assert (await client.post(path, headers=HEADERS, json=body)).status_code == 429
        # the general bucket is untouched, and the event stream is never limited
        assert (await client.get(f"/sessions/{session}", headers=HEADERS)).status_code == 200
        for _ in range(5):
            stream = await client.get(f"/runs/{run_id}/events", headers=HEADERS)
            assert stream.status_code == 200 and "run.completed" in stream.text
        clock.now += 60
        assert (
            await client.post(f"/sessions/{session}/messages", headers=HEADERS, json={"text": "x"})
        ).status_code == 202


async def test_off_disables_the_limits(tmp_path: Path) -> None:
    async for client in _limited(tmp_path, Clock(), messages=None, requests=None):
        for _ in range(50):
            assert (await client.get("/sessions", headers=HEADERS)).status_code == 200
