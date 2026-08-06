import pytest

from app.domain.models.event import ErrorEvent
from app.domain.utils.public_error import public_error_message
from app.interfaces.schemas.event import ErrorSSEEvent, EventMapper


def test_public_error_message_preserves_safe_explanations():
    assert public_error_message("The requested format is unsupported") == (
        "The requested format is unsupported"
    )


@pytest.mark.parametrize(
    ("internal_error", "public_fragment"),
    [
        ("finalization_timeout: provider deadline", "等待时限"),
        ("finalization_failed: provider unavailable", "暂时无法"),
        ("invalid_final_result: blank response", "未能生成可用"),
        ("Maximum iteration count reached, failed to complete the task", "执行上限"),
        ("1 validation error for ExecutionResult", "结果格式异常"),
    ],
)
def test_public_error_message_hides_internal_terminal_codes(
    internal_error,
    public_fragment,
):
    public_message = public_error_message(internal_error)

    assert public_fragment in public_message
    assert "finalization_timeout" not in public_message
    assert "finalization_failed" not in public_message
    assert "invalid_final_result" not in public_message
    assert "Maximum iteration" not in public_message
    assert "ExecutionResult" not in public_message


@pytest.mark.parametrize(
    "message",
    [
        "Cannot read /srv/private/tenant-a/report.csv",
        "Cannot read '/srv/private/my dataset/report.csv'",
        r"Cannot read C:\private\tenant-a\report.csv",
        r"Cannot read \\fileserver\private\tenant-a\report.csv",
        "Docker failed at https://worker.internal.example/api/v1",
    ],
)
def test_public_error_message_redacts_internal_paths(message):
    public_message = public_error_message(message)

    assert "/srv/private" not in public_message
    assert "my dataset/report.csv" not in public_message
    assert r"C:\private" not in public_message
    assert r"\\fileserver\private" not in public_message
    assert "worker.internal.example" not in public_message
    assert "Cannot read" in public_message or "Docker failed" in public_message


@pytest.mark.asyncio
async def test_error_event_mapper_redacts_historical_error_events():
    event = ErrorEvent(error="Dataset mount failed: /srv/private/tenant-a")

    mapped = await EventMapper.event_to_sse_event(event)

    assert isinstance(mapped, ErrorSSEEvent)
    assert mapped.data.error == "Dataset mount failed: [redacted path]"
    assert "/srv/private" not in mapped.model_dump_json()


@pytest.mark.parametrize(
    ("message", "credential", "expected"),
    [
        (
            "Request failed: Authorization: Bearer sk-live-abcdef",
            "sk-live-abcdef",
            "Request failed: Authorization: Bearer [redacted credential]",
        ),
        (
            "Proxy rejected authorization=Basic dXNlcjpwYXNz",
            "dXNlcjpwYXNz",
            "Proxy rejected authorization=Basic [redacted credential]",
        ),
        (
            "OpenAI api_key=sk-live-abcdef rejected the request",
            "sk-live-abcdef",
            "OpenAI api_key=[redacted credential] rejected the request",
        ),
        (
            "Provider api-key: 'key with spaces' is invalid",
            "key with spaces",
            "Provider api-key: '[redacted credential]' is invalid",
        ),
        (
            'OAuth response: "access_token": "top-secret", expired',
            "top-secret",
            'OAuth response: "access_token": "[redacted credential]", expired',
        ),
        (
            "GitHub token=github_pat_123; request rejected",
            "github_pat_123",
            "GitHub token=[redacted credential]; request rejected",
        ),
        (
            "Database password: hunter2 is invalid",
            "hunter2",
            "Database password: [redacted credential] is invalid",
        ),
        (
            "Redis passwd='hunter 2' is invalid",
            "hunter 2",
            "Redis passwd='[redacted credential]' is invalid",
        ),
        (
            "Webhook secret=my-secret was rejected",
            "my-secret",
            "Webhook secret=[redacted credential] was rejected",
        ),
    ],
)
def test_public_error_message_redacts_credentials(message, credential, expected):
    public_message = public_error_message(message)

    assert credential not in public_message
    assert public_message == expected


@pytest.mark.parametrize(
    "message",
    [
        "The tokenizer reported one token remaining",
        "Follow the password policy and secret rotation guidance",
        "The access token was missing from the request",
        "Token budget exceeded before the task completed",
    ],
)
def test_public_error_message_does_not_redact_ordinary_security_words(message):
    assert public_error_message(message) == message
