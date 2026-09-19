from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal["planning", "running", "complete", "failed"]
AgentKind = Literal["call", "web"]
AgentStatus = Literal["queued", "active", "done", "failed"]
BusinessType = Literal["mechanic", "dealer", "parts"]
CallOutcome = Literal["quote", "voicemail", "refused", "error"]
TranscriptRole = Literal["agent", "business"]


class Location(BaseModel):
    """Optional Task.location — GPS plus optional spoken/city label."""

    model_config = ConfigDict(extra="ignore")

    lat: float
    lng: float
    label: str | None = None


class Facts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    allInPrice: float | None = None
    laborRatePerHour: float | None = None
    laborHours: float | None = None
    acceptsCustomerParts: bool | None = None
    partsType: Literal["oem", "aftermarket"] | None = None
    warrantyMonths: int | None = None
    earliestSlot: str | None = None
    partPrice: float | None = None
    confidence: float


class TranscriptLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: TranscriptRole
    text: str
    t: float


class Business(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    type: BusinessType
    phone: str | None = None
    url: str | None = None


class CallInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    durationS: float
    answeredBy: str | None = None
    outcome: CallOutcome


class Agent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    taskId: str
    kind: AgentKind
    status: AgentStatus
    business: Business
    summary: str | None = None
    facts: Facts | None = None
    call: CallInfo | None = None
    transcript: list[TranscriptLine] | None = None


class ResultOption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    total: float
    breakdown: str
    agentIds: list[str]


class Result(BaseModel):
    model_config = ConfigDict(extra="ignore")

    options: list[ResultOption]
    recommendedOptionIndex: int
    recommendedAgentId: str
    why: str
    savingsVsQuote: float | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    request: str
    createdAt: str
    status: TaskStatus
    userQuote: float | None = None
    location: Location | None = None
    agents: list[Agent] = Field(default_factory=list)
    result: Result | None = None


class CreateTaskBody(BaseModel):
    """POST /tasks — spoken request plus optional browser GPS / city string."""

    model_config = ConfigDict(extra="ignore")

    request: str
    lat: float | None = None
    lng: float | None = None
    location: str | None = None


class BookBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agentId: str


class TaskUpdatedEvent(BaseModel):
    type: Literal["task.updated"] = "task.updated"
    task: Task


class AgentUpdatedEvent(BaseModel):
    type: Literal["agent.updated"] = "agent.updated"
    agent: Agent


class TaskResultEvent(BaseModel):
    type: Literal["task.result"] = "task.result"
    taskId: str
    result: Result


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str


TaskEvent = TaskUpdatedEvent | AgentUpdatedEvent | TaskResultEvent | ErrorEvent
