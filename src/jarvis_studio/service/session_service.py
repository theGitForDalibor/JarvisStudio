"""Session and Thread lifecycle management."""

import time
import uuid
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from jarvis_studio.core.observability.logger import logger


class SessionMetadata(BaseModel):
    """Metadata for an active user thread/session."""
    session_id: str = Field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:10]}")
    title: str = "New Session"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    metadata: Dict[str, str] = Field(default_factory=dict)


class SessionService:
    """Service to track and manage sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionMetadata] = {}

    def get_or_create_session(self, session_id: Optional[str] = None, title: Optional[str] = None) -> SessionMetadata:
        """Retrieve existing session or instantiate a new one."""
        if session_id and session_id in self._sessions:
            sess = self._sessions[session_id]
            sess.updated_at = time.time()
            return sess

        new_sess = SessionMetadata(
            session_id=session_id or f"sess_{uuid.uuid4().hex[:10]}",
            title=title or "Default Workspace Session",
        )
        self._sessions[new_sess.session_id] = new_sess
        logger.info(f"Created new session: {new_sess.session_id} ('{new_sess.title}')")
        return new_sess

    def list_sessions(self) -> List[SessionMetadata]:
        """Return all tracked sessions sorted by most recent."""
        return sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False


_global_session_service: Optional[SessionService] = None


def get_session_service() -> SessionService:
    """Retrieve the singleton SessionService instance."""
    global _global_session_service
    if _global_session_service is None:
        _global_session_service = SessionService()
    return _global_session_service
