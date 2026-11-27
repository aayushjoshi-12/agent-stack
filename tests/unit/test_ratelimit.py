"""P40: the token bucket behind the gateways' rate limits, and its settings."""

from __future__ import annotations

import pytest

from agent_stack.app.config import AppConfig
from agent_stack.core.errors import ConfigurationError
from agent_stack.gateways.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_burst_up_to_the_rate_passes_then_it_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(3, 60.0, clock)
    assert [limiter.take("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.take("a") == pytest.approx(20.0)  # one token back every 60/3 s
    clock.now += 5
    assert limiter.take("a") == pytest.approx(15.0)  # a refused call spends nothing


def test_the_bucket_refills_with_time_but_never_past_the_rate() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60.0, clock)
    limiter.take("a"), limiter.take("a")
    clock.now += 30
    assert limiter.take("a") == 0.0 and limiter.take("a") > 0
    clock.now += 3600  # a long idle earns a full bucket, no more
    assert [limiter.take("a") for _ in range(3)][-1] > 0


def test_each_key_has_its_own_bucket_and_none_turns_it_off() -> None:
    limiter = RateLimiter(1, 60.0, Clock())
    assert limiter.take("a") == 0.0 and limiter.take("a") > 0
    assert limiter.take("b") == 0.0
    off = RateLimiter(None)
    assert all(off.take("a") == 0.0 for _ in range(1000))


def test_rate_settings_default_parse_and_allow_off() -> None:
    defaults = AppConfig.from_env({})
    assert (defaults.rate_messages, defaults.rate_requests) == (30, 600)
    config = AppConfig.from_env(
        {"AGENT_STACK_RATE_MESSAGES": "5", "AGENT_STACK_RATE_REQUESTS": "off"}
    )
    assert (config.rate_messages, config.rate_requests) == (5, None)
    for bad in ("-1", "x", "1.5"):
        with pytest.raises(ConfigurationError, match="AGENT_STACK_RATE_MESSAGES"):
            AppConfig.from_env({"AGENT_STACK_RATE_MESSAGES": bad})
