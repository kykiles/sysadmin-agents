import uuid
from enum import Enum
from pydantic import BaseModel, Field


class Decision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto-approved"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str


class Result(BaseModel):
    task_id: str
    content: str
    success: bool = True


class ConfirmationRequest(BaseModel):
    task_id: str
    tool_name: str
    args: dict
    description: str
