"""Regression guard for the intentionally reduced AI-DataSeek API surface."""

from app.interfaces.api.routes import router


ROUTE_PATHS = {route.path for route in router.routes}


def test_dataset_analysis_and_plugin_routes_remain_available():
    expected_paths = {
        "/sessions",
        "/datasets/submissions",
        "/skills",
        "/mcp/servers",
        "/renderers",
        "/admin/resource-usage",
        "/admin/tasks",
        "/admin/mcp/servers",
        "/admin/skills",
        "/admin/datasets",
        "/admin/execution-nodes",
    }

    assert expected_paths <= ROUTE_PATHS


def test_removed_product_domains_are_not_routable():
    removed_prefixes = (
        "/a2a",
        "/browser-connections",
        "/claw",
        "/knowledge-bases",
        "/openai",
        "/scientific-sites",
        "/admin/a2a",
        "/admin/audit",
        "/admin/knowledge-bases",
        "/admin/models",
        "/admin/safety-rules",
        "/admin/sites",
        "/admin/subagents",
        "/admin/token-quotas",
        "/admin/users",
        "/admin/workspaces",
        "/api-keys",
        "/auth",
    )

    assert not any(
        path.startswith(prefix)
        for path in ROUTE_PATHS
        for prefix in removed_prefixes
    )
