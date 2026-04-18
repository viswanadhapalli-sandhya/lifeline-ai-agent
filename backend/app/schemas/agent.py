from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    user_id: str
    conversation_id: Optional[str] = None
    message: Optional[str] = ""
    mode: Literal["auto", "chat", "plan", "log", "weekly_reflection"] = "auto"
    goal: Optional[str] = None
    autonomous: bool = False
    context: Dict[str, Any] = Field(default_factory=dict)


class AgentStep(BaseModel):
    name: str
    status: Literal["ok", "skipped", "error"]
    detail: str
    output: Dict[str, Any] = Field(default_factory=dict)


class AgentUserRequest(BaseModel):
    user_id: str


class AgentResponse(BaseModel):
    summary: str
    conversation_id: Optional[str] = None
    ai_reply: str = ""
    actions: List[str] = Field(default_factory=list)
    nudges: List[str] = Field(default_factory=list)
    observed_state: Dict[str, Any] = Field(default_factory=dict)
    decision: Dict[str, Any] = Field(default_factory=dict)
    current_plans: Dict[str, Any] = Field(default_factory=dict)
    progress_summary: Dict[str, Any] = Field(default_factory=dict)
    structured_logs: Dict[str, Any] = Field(default_factory=dict)
    plan_updates: Dict[str, Any] = Field(default_factory=dict)
    weekly_reflection: Dict[str, Any] = Field(default_factory=dict)
    trace: List[AgentStep] = Field(default_factory=list)
