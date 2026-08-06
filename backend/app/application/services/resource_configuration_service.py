import logging
from datetime import UTC, datetime

from app.application.errors.exceptions import BadRequestError
from app.core.config import get_settings
from app.infrastructure.external.sandbox.node_health import (
    PAUSED_RECLAIM_MINUTES_KEY,
    RESOURCE_CONFIG_MANAGED_KEY,
    WARM_POOL_TARGET_KEY,
    check_execution_node,
    ensure_local_default_node,
)
from app.infrastructure.external.sandbox.sandbox_pool import configure_sandbox_pool


logger = logging.getLogger(__name__)


class ResourceConfigurationService:
    """Manage the safe, node-local subset of sandbox runtime configuration."""

    @staticmethod
    def _values(node) -> tuple[int, int, int]:
        settings = get_settings()
        runtime_config = dict(node.runtime_config or {})
        maximum = max(1, int(node.capacity.max_sandboxes or 1))
        warm_target = max(
            0,
            int(runtime_config.get(WARM_POOL_TARGET_KEY, settings.sandbox_pool_size)),
        )
        reclaim_minutes = max(
            1,
            int(
                runtime_config.get(
                    PAUSED_RECLAIM_MINUTES_KEY,
                    settings.sandbox_paused_destroy_after_minutes or 30,
                )
            ),
        )
        return maximum, warm_target, reclaim_minutes

    @staticmethod
    def _response(node) -> dict:
        maximum, warm_target, reclaim_minutes = ResourceConfigurationService._values(node)
        runtime_config = dict(node.runtime_config or {})
        return {
            "sandbox_max_concurrent": maximum,
            "sandbox_pool_size": warm_target,
            "sandbox_paused_destroy_after_minutes": reclaim_minutes,
            "running_sandboxes": int(node.health.running_sandboxes or 0),
            "warm_sandboxes": int(node.health.warm_sandboxes or 0),
            "paused_sandboxes": int(node.health.paused_sandboxes or 0),
            "configuration_source": (
                "admin"
                if runtime_config.get(RESOURCE_CONFIG_MANAGED_KEY)
                else "deployment"
            ),
            "browser_on_demand": True,
            "vnc_on_demand": True,
            "updated_at": node.updated_at,
        }

    async def get(self, *, refresh_health: bool = True) -> dict:
        node = await ensure_local_default_node()
        if refresh_health:
            try:
                await check_execution_node(node)
            except Exception as exc:
                logger.warning("Failed to refresh resource configuration health: %s", exc)
        return self._response(node)

    async def update(
        self,
        *,
        sandbox_max_concurrent: int | None = None,
        sandbox_pool_size: int | None = None,
        sandbox_paused_destroy_after_minutes: int | None = None,
    ) -> dict:
        node = await ensure_local_default_node()
        current_maximum, current_warm, current_reclaim = self._values(node)
        maximum = current_maximum if sandbox_max_concurrent is None else int(sandbox_max_concurrent)
        warm_target = current_warm if sandbox_pool_size is None else int(sandbox_pool_size)
        reclaim_minutes = (
            current_reclaim
            if sandbox_paused_destroy_after_minutes is None
            else int(sandbox_paused_destroy_after_minutes)
        )

        if maximum < 1 or maximum > 64:
            raise BadRequestError("沙箱并发上限必须在 1 到 64 之间")
        if warm_target < 0 or warm_target > 16:
            raise BadRequestError("预热沙箱数量必须在 0 到 16 之间")
        if warm_target >= maximum:
            raise BadRequestError("预热沙箱数量必须小于沙箱并发上限，以保留数据集分析容量")
        if reclaim_minutes < 1 or reclaim_minutes > 10080:
            raise BadRequestError("沙箱回收时间必须在 1 到 10080 分钟之间")

        old_capacity = node.capacity.model_copy(deep=True)
        old_runtime_config = dict(node.runtime_config or {})
        runtime_config = dict(old_runtime_config)
        runtime_config[RESOURCE_CONFIG_MANAGED_KEY] = True
        runtime_config[WARM_POOL_TARGET_KEY] = warm_target
        runtime_config[PAUSED_RECLAIM_MINUTES_KEY] = reclaim_minutes
        node.capacity.max_sandboxes = maximum
        node.runtime_config = runtime_config
        node.updated_at = datetime.now(UTC)
        await node.save()

        try:
            await configure_sandbox_pool(warm_target)
        except Exception:
            node.capacity = old_capacity
            node.runtime_config = old_runtime_config
            node.updated_at = datetime.now(UTC)
            await node.save()
            raise

        return await self.get(refresh_health=True)
