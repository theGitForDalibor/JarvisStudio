"""Intent Route Guard and Conversation State Machine.

Implements the conversation-state-guard specification from the AI Engineering Skills knowledge base:
1. Fast-Path Rule Guarding: Standard greetings, system queries, and status checks
   return high-quality direct replies in <50ms without invoking LLM tokens.
2. Sticky Context Anchor: Keeps focus on files, directories, or entities across multi-turn dialogs.
3. Emergency safety brake: Detects halt/stop commands immediately.
"""

import json
import logging
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


class GuardAction(str, Enum):
    """Routing decisions made by the front-line guard."""

    DIRECT_REPLY = "direct_reply"  # <50ms rule reply, skip LLM
    ENRICH_INTENT = "enrich_intent"  # Combine sticky entity context and pass through
    HALT_TASK = "halt_task"          # Emergency halt/stop
    PASS_THROUGH = "pass_through"    # Proceed to StateGraph ReAct execution


class GuardDecision(BaseModel):
    """Outcome of intent route guarding."""

    action: GuardAction = GuardAction.PASS_THROUGH
    reply_text: str | None = None
    enriched_message: str | None = None
    sticky_updates: dict[str, Any] = Field(default_factory=dict)
    matched_rule: str | None = None

    @property
    def rule_name(self) -> str | None:
        """Alias for matched_rule for backward compatibility."""
        return self.matched_rule


# Fast rule definitions: (Regex pattern, Response template)
_GREETING_PATTERNS = [
    (
        re.compile(r"^(你好|您好|hi|hello|hey|哈喽|在吗|在不在|早|早上好|晚上好)[\s!！?？~～]*$", re.IGNORECASE),
        (
            "你好！我是 **AgentStudio**，您的个人 AI Agent 工作空间助理。\n\n"
            "我已就绪并接入本地源码工程、长期记忆 (LTM)、向量知识库 (sqlite-vec) 与 HITL 安全审批机制。\n"
            "请随时向我发送任务指令（例如：*检索代码库*、*编写新功能*、*执行系统命令* 或 *保存个人偏好*）。"
        ),
        "greeting_fast_reply",
    ),
    (
        re.compile(r"^(你是谁|介绍一下自己|你的功能|你叫什么|自我介绍)[\s!！?？~～]*$", re.IGNORECASE),
        (
            "我是 **AgentStudio** 个人生产级 AI Agent 工作空间助理。\n\n"
            "### 核心能力特性：\n"
            "- 🛠️ **多源工具生态**：支持文件读写、目录扫描、终端命令执行与 MCP 跨进程集成\n"
            "- 🛡️ **HITL 安全审批**：高危操作（写文件、执行 Shell）需经您人工授权确认\n"
            "- 🧠 **长期记忆 (LTM)**：跨会话自动记住您的开发习惯与项目偏好\n"
            "- 📚 **本地向量知识库**：通过 sqlite-vec 与 SHA-256 增量哈希提供毫秒级私有代码检索\n"
            "- ⏱️ **白盒可观测性**：全生命周期链路追踪与性能度量"
        ),
        "identity_fast_reply",
    ),
    (
        re.compile(r"^(ping|test|测试|pong)[\s!！?？~～]*$", re.IGNORECASE),
        "pong 🏓 AgentStudio 运行正常，模型与工具链路已连通！",
        "ping_fast_reply",
    ),
    (
        re.compile(r"^(帮助|help|使用说明|指令清单)[\s!！?？~～]*$", re.IGNORECASE),
        (
            "### AgentStudio 常用指令与能力指引：\n\n"
            "- 🔍 **知识库检索**：*“请在 docs 知识库中检索 HITL 的设计规范”*\n"
            "- 📂 **代码与文件操作**：*“帮我查看 src/jarvis_studio/api/app.py 的路由注册”*\n"
            "- ⚡ **系统与终端**：*“运行 pytest 跑一遍测试”*\n"
            "- 🧠 **偏好沉淀**：*“保存偏好：以后前端组件统一使用 Tailwind CSS”*\n"
            "- 🛡️ **人工审批**：高危工具自动挂起，在对话窗口或 HITL 审批中心点击批准/拒绝即可。"
        ),
        "help_fast_reply",
    ),
    (
        re.compile(r"^(停止|终止|中止|别跑了|stop|cancel)[\s!！?？~～]*$", re.IGNORECASE),
        "⚠️ 已收到停止指令。当前会话操作已终止挂起。",
        "emergency_halt",
    ),
]


class IntentRouteGuard:
    """Evaluates incoming queries against rules and maintains sticky context."""

    def __init__(self) -> None:
        self._sticky_cache: dict[str, dict[str, Any]] = {}

    def get_sticky_context(self, session_id: str) -> dict[str, Any]:
        """Retrieve sticky context for the session, lazy-loading from DB if needed."""
        if session_id in self._sticky_cache:
            return self._sticky_cache[session_id]

        # Lazy-load from DB metadata
        try:
            from jarvis_studio.service.session_service import get_session_service

            sess_svc = get_session_service()
            with sess_svc._db.read_connection() as conn:
                row = conn.execute(
                    "SELECT metadata FROM chat_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None and row["metadata"]:
                    meta = json.loads(row["metadata"])
                    loaded = meta.get("sticky_context", {})
                    if isinstance(loaded, dict):
                        self._sticky_cache[session_id] = loaded
                        return loaded
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Could not lazy-load sticky context for session %s: %s", session_id, exc)

        self._sticky_cache[session_id] = {}
        return self._sticky_cache[session_id]

    def update_sticky_context(self, session_id: str, updates: dict[str, Any]) -> None:
        """Update sticky context for the session and write through to DB metadata."""
        if session_id not in self._sticky_cache:
            self.get_sticky_context(session_id)
        self._sticky_cache[session_id].update(updates)
        self._write_through(session_id)

    def clear_sticky_context(self, session_id: str) -> None:
        """Clear sticky context from memory and DB metadata."""
        self._sticky_cache.pop(session_id, None)
        try:
            from jarvis_studio.service.session_service import get_session_service

            sess_svc = get_session_service()
            with sess_svc._db.transaction() as conn:
                row = conn.execute(
                    "SELECT metadata FROM chat_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None and row["metadata"]:
                    meta = json.loads(row["metadata"])
                    if "sticky_context" in meta:
                        del meta["sticky_context"]
                        conn.execute(
                            "UPDATE chat_sessions SET metadata = ? WHERE session_id = ?",
                            (json.dumps(meta), session_id),
                        )
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Could not clear sticky context in DB for session %s: %s", session_id, exc)

    def _write_through(self, session_id: str) -> None:
        """Write current sticky context back to session metadata in DB."""
        try:
            from jarvis_studio.service.session_service import get_session_service

            sess_svc = get_session_service()
            with sess_svc._db.transaction() as conn:
                row = conn.execute(
                    "SELECT metadata FROM chat_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is not None:
                    meta = {}
                    if row["metadata"]:
                        try:
                            meta = json.loads(row["metadata"])
                        except (json.JSONDecodeError, TypeError):
                            meta = {}
                    meta["sticky_context"] = self._sticky_cache.get(session_id, {})
                    conn.execute(
                        "UPDATE chat_sessions SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id),
                    )
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Could not write-through sticky context for session %s: %s", session_id, exc)

    def evaluate(self, message: str, session_id: str) -> GuardDecision:
        """Execute the 4-branch intent guarding decision."""
        clean_text = message.strip()

        # Branch 1 & 2: Fast-path rule matching (<50ms, skip LLM entirely)
        for pattern, reply_text, rule_name in _GREETING_PATTERNS:
            if pattern.match(clean_text):
                action = GuardAction.HALT_TASK if rule_name == "emergency_halt" else GuardAction.DIRECT_REPLY
                return GuardDecision(
                    action=action,
                    reply_text=reply_text,
                    matched_rule=rule_name,
                )

        # Branch 3: Sticky Entity Context Enrichment
        # Detect if user mentions a specific file or path in this message
        file_match = re.search(r"([a-zA-Z0-9_\-\./\\]+\.(?:py|md|json|html|css|js|ts|ya?ml|toml))", clean_text)
        sticky_updates = {}
        if file_match:
            detected_file = file_match.group(1)
            sticky_updates["active_file"] = detected_file
            self.update_sticky_context(session_id, sticky_updates)

        sticky = self.get_sticky_context(session_id)
        # If user asks vague follow-up referring to "it" or line numbers and we have a sticky file
        vague_followup = re.search(r"^(它|这篇|这个文件|第\s*\d+\s*行|看一下它|重构一下|解释一下)", clean_text)
        if vague_followup and "active_file" in sticky:
            active_file = sticky["active_file"]
            enriched = f"[上下文锚定文件: {active_file}]\n{clean_text}"
            return GuardDecision(
                action=GuardAction.ENRICH_INTENT,
                enriched_message=enriched,
                sticky_updates=sticky_updates,
                matched_rule="sticky_anchor_enrichment",
            )

        # Branch 4: Pass through to LangGraph ReAct execution
        return GuardDecision(
            action=GuardAction.PASS_THROUGH,
            sticky_updates=sticky_updates,
            matched_rule="pass_through_react",
        )


_global_guard: IntentRouteGuard | None = None


def get_route_guard() -> IntentRouteGuard:
    """Retrieve singleton IntentRouteGuard instance."""
    global _global_guard
    if _global_guard is None:
        _global_guard = IntentRouteGuard()
    return _global_guard
