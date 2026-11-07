import time

import pytest

from runner_spike.fake_model import FakeModelError, FakeStreamingChatModel


async def stream_tokens(model: FakeStreamingChatModel) -> list[str]:
    return [chunk.text async for chunk in model.astream("prompt")]


async def test_streams_response_token_by_token_without_losing_whitespace() -> None:
    tokens = await stream_tokens(FakeStreamingChatModel(response="one two  three"))

    assert [token for token in tokens if token] == ["one ", "two  ", "three"]
    assert "".join(tokens) == "one two  three"


async def test_delay_applies_per_token() -> None:
    model = FakeStreamingChatModel(response="a b c d", token_delay_seconds=0.02)

    started_at = time.monotonic()
    await stream_tokens(model)

    assert time.monotonic() - started_at >= 0.07


async def test_failure_rate_of_one_always_fails() -> None:
    for seed in range(20):
        model = FakeStreamingChatModel(response="a b", failure_rate=1.0, seed=seed)
        with pytest.raises(FakeModelError):
            await stream_tokens(model)


async def test_failure_rate_of_zero_never_fails() -> None:
    for seed in range(20):
        model = FakeStreamingChatModel(response="a b", failure_rate=0.0, seed=seed)
        assert "".join(await stream_tokens(model)) == "a b"


async def test_failure_rate_is_honoured_and_reproducible_by_seed() -> None:
    async def fails(seed: int) -> bool:
        model = FakeStreamingChatModel(response="a", failure_rate=0.3, seed=seed)
        try:
            await stream_tokens(model)
        except FakeModelError:
            return True
        return False

    outcomes = [await fails(seed) for seed in range(300)]

    assert 0.2 < sum(outcomes) / len(outcomes) < 0.4
    assert outcomes == [await fails(seed) for seed in range(300)]


async def test_can_fail_midway_through_a_stream() -> None:
    model = FakeStreamingChatModel(response="a b c", failure_rate=1.0, fail_after_tokens=2)
    received: list[str] = []

    with pytest.raises(FakeModelError, match="after 2 tokens"):
        async for chunk in model.astream("prompt"):
            received.append(chunk.text)

    assert "".join(received) == "a b "
