"""P0-4 等待策略：开关与内置工具分类。

只依赖 settings（不依赖 ORM/路由），供 graph / assembler / runtime 复用，
避免引入循环导入。
"""

from app.core.config import settings

# 派发类内置工具：调用即"外部流程已开始"，平台接管等待（工具结果延后到 await_gate）
AWAIT_DISPATCH_BUILTINS = frozenset({"procurement_trigger"})

# 被平台等待取代的轮询类内置工具：平台持有等待后不再暴露给模型（模型零轮询）
AWAIT_SUPERSEDED_BUILTINS = frozenset({"procurement_status"})


def await_enabled() -> bool:
    """灰度开关：外部服务实现回调契约前保持 False，行为与旧版完全一致。"""
    return bool(settings.await_external_enabled)


def is_dispatch_builtin(name: str | None) -> bool:
    return bool(name) and name in AWAIT_DISPATCH_BUILTINS


def is_model_hidden_builtin(name: str | None) -> bool:
    """是否应从模型可见工具列表中移除（仅开关打开时生效）。"""
    return await_enabled() and bool(name) and name in AWAIT_SUPERSEDED_BUILTINS
