"""全量模型注册：任何需要完整 metadata 的入口（应用启动/Alembic）都应导入本模块。"""

from app.modules.agents.models import Agent
from app.modules.auth.models import User
from app.modules.capabilities.models import Capability, CapabilityBinding, CapabilityTool
from app.modules.conversations.models import Conversation, Message
from app.modules.engine.models import InboxEvent, Plan
from app.modules.files.models import File, FileRef
from app.modules.knowledge.models import KbChunk, KbDoc, KbFolder
from app.modules.memory.models import MemoryFile
from app.modules.models_module.models import ModelProvider
from app.modules.notifications.models import Notification
from app.modules.runs.models import Run, RunArtifact, RunEvent
from app.modules.scheduler.models import Alarm, Timer
from app.modules.skills_forge.models import SkillProposal
from app.modules.tasks.models import (
    Task,
    TaskStep,
)

__all__ = [
    "Agent",
    "Alarm",
    "Capability",
    "CapabilityBinding",
    "CapabilityTool",
    "Conversation",
    "File",
    "FileRef",
    "InboxEvent",
    "KbChunk",
    "KbDoc",
    "KbFolder",
    "MemoryFile",
    "Message",
    "ModelProvider",
    "Notification",
    "Plan",
    "Run",
    "RunArtifact",
    "RunEvent",
    "SkillProposal",
    "Task",
    "TaskStep",
    "Timer",
    "User",
]
