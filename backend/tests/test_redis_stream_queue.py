import logging

import pytest

from app.infrastructure.external.message_queue.redis_stream_queue import RedisStreamQueue


class FakeRedisClient:
    def __init__(self):
        self.calls = []

    async def xread(self, streams, count, block):
        self.calls.append((streams, count, block))
        return []


class FakeWriteRedisClient:
    async def xadd(self, stream_name, payload):
        assert stream_name == "task:output:test"
        assert "data" in payload
        return "1710000000000-4"


class FakeDeleteRedisClient:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count

    async def xdel(self, stream_name, message_id):
        assert stream_name == "task:input:test"
        assert message_id == "1710000000000-3"
        return self.deleted_count


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


@pytest.mark.asyncio
async def test_put_logs_only_payload_shape_not_raw_event(caplog):
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:output:test"
    queue._redis = type("RedisHolder", (), {"client": FakeWriteRedisClient()})()
    payload = '{"message":"sk-private-value /Users/alice/private.csv"}'

    with caplog.at_level(logging.DEBUG):
        event_id = await queue.put(payload)

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert event_id == "1710000000000-4"
    assert "payload_type=str" in rendered
    assert "payload_bytes=" in rendered
    assert "sk-private-value" not in rendered
    assert "/Users/alice/private.csv" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deleted_count", "expected"),
    [(1, True), (0, False)],
)
async def test_delete_message_reports_whether_the_entry_existed(deleted_count, expected):
    queue = object.__new__(RedisStreamQueue)
    queue._stream_name = "task:input:test"
    queue._redis = type(
        "RedisHolder",
        (),
        {"client": FakeDeleteRedisClient(deleted_count)},
    )()

    deleted = await queue.delete_message("1710000000000-3")

    assert deleted is expected
