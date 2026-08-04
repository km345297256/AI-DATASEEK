import pytest

from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue


class FakeRedisClient:
    def __init__(self):
        self.calls = []

    async def xread(self, streams, count, block):
        self.calls.append((streams, count, block))
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", ["", "undefined", "event-uuid", "None"])
async def test_get_normalizes_invalid_stream_ids(invalid_id):
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:output:test"
    queue._redis = type("RedisHolder", (), {"client": FakeRedisClient()})()

    result = await queue.get(start_id=invalid_id, block_ms=0)

    assert result == (None, None)
    assert queue._redis.client.calls[0][0] == {"task:output:test": "0-0"}


@pytest.mark.asyncio
async def test_get_preserves_valid_stream_id():
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:output:test"
    queue._redis = type("RedisHolder", (), {"client": FakeRedisClient()})()

    await queue.get(start_id="1710000000000-3", block_ms=0)

    assert queue._redis.client.calls[0][0] == {"task:output:test": "1710000000000-3"}
