"""Business-level Thinking State Machine and Stream Protocol Engine.

Implements the pseudo-thinking-stream specification from the AI Engineering Skills knowledge base:
1. 4-phase lifecycle: understand -> tool -> organize -> completed
2. White-box translation of internal tool names to user-friendly status labels
3. Heartbeat keepalive rotation (8-10s) to prevent proxy/TCP idle timeout
4. Fingerprint deduplication and anti-inversion atomic protocol
"""

import asyncio
import time
from enum import Enum
from typing import Any, AsyncGenerator

from jarvis_studio.core.observability.logger import logger


class ThinkingPhase(str, Enum):
    """The 4-stage lifecycle of business-level thinking."""

    UNDERSTAND = "understand"  # Phase 1: Immediate understanding (<100ms)
    TOOL = "tool"              # Phase 2: Tool execution with white-box translation
    ORGANIZE = "organize"      # Phase 3: Long-running synthesis with keepalive heartbeat
    COMPLETED = "completed"    # Phase 4: Atomic finalization


# White-box translation mapping from technical tool names to user-friendly messages
TOOL_LABEL_MAP: dict[str, str] = {
    "read_file": "正在读取工作区源码文件...",
    "list_directory": "正在扫描工作区目录结构...",
    "write_file": "正在写入文件变更...",
    "execute_shell_command": "正在终端准备执行命令...",
    "save_personal_memory": "正在将偏好保存至长期记忆 (LTM)...",
    "recall_personal_memory": "正在检索长期个人记忆与历史约定...",
    "search_knowledge_base": "正在检索本地向量知识库 (sqlite-vec)...",
    "index_workspace_directory": "正在扫描并增量构建向量索引...",
    "list_indexed_documents": "正在获取已索引的知识库文档列表...",
}


class ThinkingEngine:
    """Manages structured thinking events, keepalive heartbeats, and token integration."""

    def __init__(self, session_id: str, heartbeat_interval: float = 8.0) -> None:
        self.session_id = session_id
        self.heartbeat_interval = heartbeat_interval
        self._current_phase = ThinkingPhase.UNDERSTAND
        self._is_active = True
        self._heartbeat_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._start_time = time.time()
        self._last_fingerprint = ""

    def get_elapsed_ms(self) -> int:
        """Milliseconds since thinking started."""
        return int((time.time() - self._start_time) * 1000)

    def _format_event(
        self, text: str, status: str, phase: ThinkingPhase, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build standard thinking event payload with fingerprint."""
        fingerprint = f"{phase.value}|{status}|{text}"
        return {
            "event": "thinking",
            "data": {
                "session_id": self.session_id,
                "phase": phase.value,
                "status": status,  # "start" | "running" | "completed"
                "text": text,
                "elapsed_ms": self.get_elapsed_ms(),
                "fingerprint": fingerprint,
                "details": details or {},
            },
        }

    async def emit_understand(self, text: str = "正在理解您的指令与上下文...") -> None:
        """Phase 1: Emit immediate understanding event (<100ms first token experience)."""
        self._current_phase = ThinkingPhase.UNDERSTAND
        event = self._format_event(text, "start", ThinkingPhase.UNDERSTAND)
        await self._event_queue.put(event)

    async def emit_tool_start(self, tool_name: str, tool_args: Any = None) -> None:
        """Phase 2: Emit translated tool execution start event."""
        self._current_phase = ThinkingPhase.TOOL
        friendly_label = TOOL_LABEL_MAP.get(tool_name, f"正在执行工具 [{tool_name}]...")
        event = self._format_event(
            friendly_label,
            "running",
            ThinkingPhase.TOOL,
            details={"tool_name": tool_name, "arguments": tool_args},
        )
        await self._event_queue.put(event)

    async def emit_tool_end(self, tool_name: str, output_snippet: str = "") -> None:
        """Phase 2: Emit tool completion step."""
        friendly_label = TOOL_LABEL_MAP.get(tool_name, f"工具 [{tool_name}] 执行完毕")
        event = self._format_event(
            f"✓ {friendly_label}",
            "running",
            ThinkingPhase.TOOL,
            details={"tool_name": tool_name, "snippet": output_snippet[:200]},
        )
        await self._event_queue.put(event)

    def start_heartbeat(self) -> None:
        """Phase 3: Start background keepalive rotation when model synthesizes text."""
        self._current_phase = ThinkingPhase.ORGANIZE
        if not self._heartbeat_task or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Rotates friendly keepalive messages every heartbeat_interval seconds."""
        rotation_messages = [
            "正在梳理分析资料与上下文...",
            "正在组织结构化回复...",
            "分析较深，请稍候...",
        ]
        idx = 0
        try:
            while self._is_active:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._is_active:
                    break
                text = rotation_messages[idx % len(rotation_messages)]
                idx += 1
                event = self._format_event(text, "running", ThinkingPhase.ORGANIZE)
                await self._event_queue.put(event)
        except asyncio.CancelledError:
            pass

    async def emit_completed(self, summary: str = "思考与工具执行完毕") -> None:
        """Phase 4: Finalize thinking stage."""
        self._is_active = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        self._current_phase = ThinkingPhase.COMPLETED
        event = self._format_event(
            summary,
            "completed",
            ThinkingPhase.COMPLETED,
            details={"total_duration_ms": self.get_elapsed_ms()},
        )
        await self._event_queue.put(event)

    def close(self) -> None:
        """Shut down engine and any background tasks."""
        self._is_active = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def get_queued_events(self) -> list[dict[str, Any]]:
        """Drain currently pending events without blocking."""
        events = []
        while not self._event_queue.empty():
            events.append(self._event_queue.get_nowait())
        return events
