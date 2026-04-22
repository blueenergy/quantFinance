from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum

class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_PARSE_ERROR = "completed_with_parse_error"
    FAILED = "failed"

class AnalysisTaskCreate(BaseModel):
    symbol: str
    priority: int = Field(default=1, description="1=User, 0=Batch")
    provider: Optional[str] = None
    model: Optional[str] = None
    feedback: Optional[str] = None
    revision_of: Optional[str] = None

class AnalysisTask(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id", description="MongoDB ObjectId")
    user_id: str
    symbol: str
    task_type: Optional[str] = None
    created_by_user_id: Optional[str] = None
    created_by_username: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 1
    created_at: datetime
    updated_at: datetime
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    revision_of: Optional[str] = None
    feedback: Optional[str] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
