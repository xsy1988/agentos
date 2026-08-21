"""全量模型注册：任何需要完整 metadata 的入口（应用启动/Alembic）都应导入本模块。"""

from app.modules.agents.models import Agent
from app.modules.auth.models import User
from app.modules.capabilities.models import Capability, CapabilityBinding, CapabilityTool
from app.modules.knowledge.models import KbFolder
from app.modules.models_module.models import ModelProvider
from app.modules.scheduler.models import Alarm, Timer

__all__ = [
    "Agent",
    "Alarm",
    "Capability",
    "CapabilityBinding",
    "CapabilityTool",
    "KbFolder",
    "ModelProvider",
    "Timer",
    "User",
]
