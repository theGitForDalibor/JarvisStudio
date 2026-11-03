"""Data models for Long-Term Memory (LTM)."""

import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class MemoryCategory(str, Enum):
    """Categorization of user memories."""

    PREFERENCE = "preference"  # User habit, coding style, tool choice
    PROFILE = "profile"  # Developer role, OS, hardware, environment
    PROJECT = "project"  # Workspace, architecture constraints, naming rules
    FACT = "fact"  # General static knowledge or facts provided by user


class MemoryItem(BaseModel):
    """A single persistent memory record."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    category: MemoryCategory = Field(default=MemoryCategory.PREFERENCE)
    content: str = Field(..., description="The factual or behavioral memory content")
    source: str | None = Field(
        default=None, description="Origin session_id or 'manual'"
    )
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    importance: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Priority weight (0.0 - 1.0)"
    )
