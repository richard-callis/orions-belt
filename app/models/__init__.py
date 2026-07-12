# Import all models so SQLAlchemy registers them with the metadata
from app.models.auth import User
from app.models.settings import Setting
from app.models.chat import Session, Message, ContextCompaction
from app.models.chat_approval import PendingToolApproval
from app.models.work import Project, Epic, Feature, Task
from app.models.agent import Agent, AgentRun, AgentStep, TokenUsage
from app.models.knowledge import Note
from app.models.connector import Connector, AuthorizedDirectory
from app.models.mcp_tool import MCPTool, ToolProposal
from app.models.memory import Memory
from app.models.pii import PIIHashEntry
from app.models.logs import AuditLog, PIILog, AgentLog, LLMLog, AgentTrace
from app.models.chat_room_goal import ChatRoomGoal

__all__ = [
    "User",
    "Setting",
    "Session", "Message", "ContextCompaction",
    "PendingToolApproval",
    "Project", "Epic", "Feature", "Task",
    "Agent", "AgentRun", "AgentStep", "TokenUsage",
    "Note",
    "Connector", "AuthorizedDirectory",
    "MCPTool", "ToolProposal",
    "Memory",
    "PIIHashEntry",
    "AuditLog", "PIILog", "AgentLog", "LLMLog", "AgentTrace",
    "ChatRoomGoal",
]
