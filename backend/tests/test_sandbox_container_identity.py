from app.infrastructure.external.sandbox.container_identity import (
    is_sandbox_container_name,
)


def test_project_specific_sandbox_prefix_is_recognized():
    assert is_sandbox_container_name(
        "ai-dataseek-sandbox-a1b2c3d4",
        "ai-dataseek-sandbox",
    )
    assert is_sandbox_container_name(
        "/ai-dataseek-sandbox-b2c3d4e5",
        "ai-dataseek-sandbox",
    )


def test_legacy_prefix_remains_recognized_after_upgrade():
    assert is_sandbox_container_name("sandbox-a1b2c3d4", "ai-dataseek-sandbox")


def test_unrelated_containers_are_not_counted_as_sandboxes():
    assert not is_sandbox_container_name("mongodb", "ai-dataseek-sandbox")
    assert not is_sandbox_container_name("ai-dataseek-sandbox-image", "ai-dataseek-sandbox")
    assert not is_sandbox_container_name(None, "ai-dataseek-sandbox")
