from agent_stack.app.bootstrap import DEFAULT_SYSTEM_PROMPT
from agent_stack.core.runtime import SPIRAL_THRESHOLD, count_second_guessing


def test_counts_second_guessing() -> None:
    assert count_second_guessing("WAIT WAIT. Actually, hmm. But wait, no") >= 5
    assert count_second_guessing("Read the file, edit it, run the tests.") == 0


def test_a_spiral_crosses_the_threshold() -> None:
    assert count_second_guessing("Wait, actually. " * SPIRAL_THRESHOLD) >= SPIRAL_THRESHOLD


def test_prompt_asks_for_verification_before_stopping() -> None:
    assert "awkward cases first" in DEFAULT_SYSTEM_PROMPT


def test_prompt_keeps_results_and_waits_on_long_jobs() -> None:
    assert "Leave your results in place" in DEFAULT_SYSTEM_PROMPT
    assert "sleeps 60-120 seconds" in DEFAULT_SYSTEM_PROMPT
