# Import all models so SQLAlchemy registers them with the metadata
from app.models.settings import Setting
from app.models.chat import Session, Message, ContextCompaction
from app.models.work import Project, Epic, Feature, Task
from app.models.agent import Agent, AgentRun, AgentStep
from app.models.connector import Connector, AuthorizedDirectory
from app.models.mcp_tool import MCPTool, ToolProposal
from app.models.memory import Memory
from app.models.pii import PIIHashEntry
from app.models.logs import AuditLog, PIILog, AgentLog, LLMLog

__all__ = [
    "Setting",
    "Session", "Message", "ContextCompaction",
    "Project", "Epic", "Feature", "Task",
    "Agent", "AgentRun", "AgentStep",
    "Connector", "AuthorizedDirectory",
    "MCPTool", "ToolProposal",
    "Memory",
    "PIIHashEntry",
    "AuditLog", "PIILog", "AgentLog", "LLMLog",
]
