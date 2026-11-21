"""High-level Agent Application Service decoupling API/UI from LangGraph."""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.agent.hitl.manager import (
    ApprovalDecision,
    ApprovalStatus,
    HITLManager,
    compute_arguments_hash,
    get_hitl_manager,
)
from jarvis_studio.core.guard.route_guard import GuardAction, GuardDecision, get_route_guard
from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.observability.trace_logger import (
    ConversationTrace,
    get_trace_store,
)
from jarvis_studio.core.observability.tracer import set_trace_context, timed_step
from jarvis_studio.core.tasks.coordinator import TaskCoordinator, get_task_coordinator
from jarvis_studio.core.tasks.models import (
    AgentTask,
    EventType,
    RunTriggerType,
    TaskStatus,
)
from jarvis_studio.core.thinking.engine import ThinkingEngine
from jarvis_studio.service.session_service import SessionService, get_session_service


@dataclass
class _ThreadLockEntry:
    """Per-thread async lock paired with active waiter/holder reference count."""

    lock: asyncio.Lock
    waiters: int = 0


class _ThreadLockGuard:
    """Async context manager serializing execution per `(loop, thread_id)`.

    Implemented directly via `__aenter__` / `__aexit__` rather than `@asynccontextmanager`
    so early async-generator close (`GeneratorExit`) never hits `athrow()` impedance errors.
    """

    def __init__(
        self,
        buckets: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _ThreadLockEntry]],
        loop: asyncio.AbstractEventLoop,
        key: str,
    ) -> None:
        self._buckets = buckets
        self._loop = loop
        self._key = key
        self._loop_locks: dict[str, _ThreadLockEntry] | None = None
        self._entry: _ThreadLockEntry | None = None
        self._acquired = False

    async def __aenter__(self) -> _ThreadLockEntry:
        loop_locks = self._buckets.setdefault(self._loop, {})
        entry = loop_locks.get(self._key)
        if entry is None:
            entry = _ThreadLockEntry(lock=asyncio.Lock(), waiters=0)
            loop_locks[self._key] = entry

        # Increment waiters synchronously before awaiting acquire so concurrent releases
        # never pop an active lock entry and split into two Lock instances.
        entry.waiters += 1
        self._loop_locks = loop_locks
        self._entry = entry
        try:
            await entry.lock.acquire()
            self._acquired = True
            return entry
        except BaseException:
            self._release_entry()
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._release_entry()
        return False

    def _release_entry(self) -> None:
        entry = self._entry
        loop_locks = self._loop_locks
        if entry is None or loop_locks is None:
            return
        self._entry = None
        self._loop_locks = None
        if self._acquired:
            self._acquired = False
            entry.lock.release()
        entry.waiters -= 1
        if entry.waiters == 0 and loop_locks.get(self._key) is entry:
            loop_locks.pop(self._key, None)


def _extract_checkpoint_id(state_snapshot: Any) -> str | None:
    if state_snapshot is None:
        return None
    cfg = getattr(state_snapshot, "config", None)
    if isinstance(cfg, dict):
        configurable = cfg.get("configurable", {})
        if isinstance(configurable, dict):
            cp_id = configurable.get("checkpoint_id")
            if cp_id:
                return str(cp_id)
    return None


class AgentExecutionResult:
    """Encapsulates the outcome of an agent invocation."""

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        status: str,  # "completed" | "interrupted" | "error"
        output_text: str = "",
        pending_approval: dict[str, Any] | None = None,
        error: str | None = None,
        task_id: str | None = None,
    ):
        self.thread_id = thread_id
        self.run_id = run_id
        self.status = status
        self.output_text = output_text
        self.pending_approval = pending_approval
        self.error = error
        self.task_id = task_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "output_text": self.output_text,
            "pending_approval": self.pending_approval,
            "error": self.error,
        }


class AgentService:
    """Service orchestrating agent executions, checkpoints, and HITL resumptions."""

    def __init__(
        self,
        graph=None,
        *,
        registry=None,
        checkpointer=None,
        session_service: SessionService | None = None,
        hitl_manager: HITLManager | None = None,
        task_coordinator: TaskCoordinator | None = None,
        trace_store: Any | None = None,
        bound_loop: asyncio.AbstractEventLoop | None = None,
        strict_loop_binding: bool | None = None,
        run_begin_hook: Callable[[], asyncio.Task[Any] | None] | None = None,
        run_end_hook: Callable[[asyncio.Task[Any] | None], None] | None = None,
    ):
        self.session_service = session_service or get_session_service()
        self.hitl_manager = hitl_manager or get_hitl_manager()
        self._explicit_task_coordinator = task_coordinator is not None
        if task_coordinator is not None:
            self._task_coordinator = task_coordinator
        elif self.session_service is not None:
            self._task_coordinator = TaskCoordinator(db_path=self.session_service.db_path)
        else:
            self._task_coordinator = get_task_coordinator()
        self.trace_store = trace_store
        self.graph = graph or build_agent_graph(
            registry=registry, checkpointer=checkpointer, hitl_mgr=self.hitl_manager
        )
        self._bound_loop: asyncio.AbstractEventLoop | None = bound_loop
        self._explicit_strict_loop_binding: bool | None = strict_loop_binding
        self._run_begin_hook = run_begin_hook
        self._run_end_hook = run_end_hook
        self._thread_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _ThreadLockEntry]] = WeakKeyDictionary()

    @property
    def task_coordinator(self) -> TaskCoordinator:
        if self._task_coordinator is None:
            db_path = self.session_service.db_path if self.session_service else None
            self._task_coordinator = TaskCoordinator(db_path=db_path)
        elif (
            not self._explicit_task_coordinator
            and self.session_service is not None
            and getattr(self._task_coordinator, "db_path", None) != self.session_service.db_path
        ):
            self._task_coordinator = TaskCoordinator(db_path=self.session_service.db_path)
        return self._task_coordinator

    @task_coordinator.setter
    def task_coordinator(self, value: TaskCoordinator) -> None:
        self._task_coordinator = value
        self._explicit_task_coordinator = True

    def _begin_runtime_run(self) -> asyncio.Task[Any] | None:
        if self._run_begin_hook is not None:
            return self._run_begin_hook()
        return None

    def _end_runtime_run(self, task_token: asyncio.Task[Any] | None) -> None:
        if self._run_end_hook is not None:
            self._run_end_hook(task_token)

    def _uses_async_sqlite_checkpointer(self) -> bool:
        """Return True when the active graph checkpointer is AsyncSqliteSaver."""
        cp = getattr(self.graph, "checkpointer", None)
        return cp is not None and type(cp).__name__ == "AsyncSqliteSaver"

    def _is_strict_loop_enforced(self) -> bool:
        """Production and AsyncSqliteSaver services must stay bound to a single loop."""
        if self._uses_async_sqlite_checkpointer():
            return True
        if self._explicit_strict_loop_binding is not None:
            return self._explicit_strict_loop_binding
        return self._bound_loop is not None

    def _verify_event_loop(self) -> asyncio.AbstractEventLoop:
        """Enforce single-loop execution when bound to AgentRuntime or AsyncSqliteSaver."""
        running_loop = asyncio.get_running_loop()
        strict = self._is_strict_loop_enforced()
        if self._bound_loop is None:
            if strict:
                self._bound_loop = running_loop
            return running_loop
        if running_loop is not self._bound_loop and strict:
            raise RuntimeError(
                "AgentService invoked on mismatched event loop: "
                f"bound_loop={id(self._bound_loop)}, current_loop={id(running_loop)}"
            )
        return running_loop

    @staticmethod
    def _normalize_lock_key(thread_id: str) -> str:
        if isinstance(thread_id, str):
            stripped = thread_id.strip()
            if stripped:
                return stripped
        return str(thread_id)

    def _acquire_thread_lock(self, thread_id: str) -> "_ThreadLockGuard":
        """Create a loop-verified, cancel-safe async lock guard for `thread_id`."""
        loop = self._verify_event_loop()
        key = self._normalize_lock_key(thread_id)
        return _ThreadLockGuard(self._thread_locks, loop, key)

    def is_session_busy(self, session_id: str) -> bool:
        """Check whether the session has active tasks, pending approvals, or in-flight executions."""
        return self.session_service.is_session_busy(session_id)

    def rebuild_graph(self, registry=None, model=None, checkpointer=None) -> None:
        """Rebuild and recompile the StateGraph with updated tools or configuration."""
        self.graph = build_agent_graph(
            model=model,
            registry=registry,
            checkpointer=checkpointer,
            hitl_mgr=self.hitl_manager,
        )

    def _validate_task_executable(
        self, task_id: str, *, allow_queued: bool = False
    ) -> AgentTask:
        """Validate that an explicit task exists, is unarchived, and is eligible for execution."""
        task = self.task_coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.archived_at is not None:
            raise ValueError(f"Task '{task_id}' is archived and cannot be executed")
        if task.status == TaskStatus.UNKNOWN:
            raise RuntimeError(
                f"Task '{task_id}' is in UNKNOWN state due to uncertain external side effects "
                "and cannot be automatically re-executed."
            )
        blocked = [
            TaskStatus.RUNNING,
            TaskStatus.EXECUTING_TOOL,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLING,
        ]
        if not allow_queued:
            blocked.append(TaskStatus.QUEUED)
        if task.status in blocked:
            raise ValueError(
                f"Task '{task_id}' cannot be executed while in state '{task.status.value}'"
            )
        return task

    def _safe_record_event(
        self,
        task_id: str,
        event_type: EventType,
        *,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist observable task lifecycle event without crashing if coordinator lacks table/hook."""
        try:
            self.task_coordinator.record_event(
                task_id=task_id,
                event_type=event_type,
                run_id=run_id,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not record event {event_type.value} for task {task_id}: {exc}")

    def _resolve_tool_step_type(self, tool_name: str) -> str:
        """Distinguish external MCP tools from local built-in tools for trace steps."""
        try:
            tool = self.registry.get_tool(tool_name)
            if tool is not None:
                if hasattr(tool, "server_name"):
                    return "mcp"
                meta = self.registry.get_metadata(tool_name)
                if meta is not None and str(meta.category).lower() == "mcp":
                    return "mcp"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Could not resolve tool step type for '{tool_name}': {exc}")
        return "tool"

    @staticmethod
    def _preload_rag_context_sync(
        query: str,
        knowledge_scope: dict[str, Any] | None,
        *,
        top_k: int = 3,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Synchronously query local RAG store for speculative preloading (fail-open)."""
        from pathlib import Path

        from jarvis_studio.config.settings import get_settings
        from jarvis_studio.rag.embeddings import EmbeddingsFactory
        from jarvis_studio.rag.store import SqliteVecStore

        settings = get_settings()
        if not settings.rag.enabled or not query.strip():
            return "", []

        vec_path = Path(settings.rag.vector_db_path)
        if not vec_path.exists():
            return "", []

        cols: list[str] | None = None
        doc_ids: list[str] | None = None
        if isinstance(knowledge_scope, dict):
            cols = [str(c) for c in (knowledge_scope.get("collections") or []) if str(c).strip()]
            doc_ids = [str(d) for d in (knowledge_scope.get("doc_ids") or []) if str(d).strip()]
            if not cols and not doc_ids:
                return "", []

        store = SqliteVecStore()
        try:
            docs = store.list_documents(collections=cols or None, doc_ids=doc_ids or None)
            if not docs:
                return "", []
            embeddings = EmbeddingsFactory.create_embeddings()
            query_vec = embeddings.embed_query(query)
            matches = store.search(
                query_embedding=query_vec,
                top_k=top_k,
                query_text=query,
                collections=cols or None,
                doc_ids=doc_ids or None,
            )
        finally:
            store.close()

        if not matches:
            return "", []

        lines: list[str] = []
        sources: list[dict[str, Any]] = []
        for idx, m in enumerate(matches, start=1):
            doc_path = m.get("doc_path") or m.get("doc_id") or "unknown"
            snippet = str(m.get("content") or "").strip()[:400]
            if not snippet:
                continue
            lines.append(f"[{idx}] {doc_path}: {snippet}")
            sources.append(
                {
                    "doc_id": m.get("doc_id"),
                    "doc_path": doc_path,
                    "score": m.get("score"),
                }
            )
        return "\n".join(lines), sources

    async def _preload_rag_context(
        self,
        query: str,
        knowledge_scope: dict[str, Any] | None,
        *,
        timeout_seconds: float = 1.2,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run lightweight speculative RAG preload with a strict timeout (fail-open)."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._preload_rag_context_sync,
                    query,
                    knowledge_scope,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Speculative RAG preload skipped: {exc}")
            return "", []

    @staticmethod
    async def _iter_graph_with_thinking(
        graph_stream: AsyncGenerator[dict[str, Any], None],
        thinking_engine: ThinkingEngine,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """Yield ('graph', ev) and ('thinking', ev) concurrently so heartbeats flush in real time."""
        graph_iter = graph_stream.__aiter__()
        next_graph: asyncio.Task[dict[str, Any]] | None = asyncio.create_task(graph_iter.__anext__())
        next_think: asyncio.Task[dict[str, Any]] | None = asyncio.create_task(
            thinking_engine._event_queue.get()
        )
        try:
            while next_graph is not None:
                wait_set = {next_graph}
                if next_think is not None:
                    wait_set.add(next_think)
                done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

                if next_think in done:
                    try:
                        think_ev = next_think.result()
                        yield ("thinking", think_ev)
                    except asyncio.CancelledError:
                        pass
                    next_think = asyncio.create_task(thinking_engine._event_queue.get())

                if next_graph in done:
                    try:
                        graph_ev = next_graph.result()
                        yield ("graph", graph_ev)
                        next_graph = asyncio.create_task(graph_iter.__anext__())
                    except StopAsyncIteration:
                        next_graph = None
        finally:
            for pending in (next_graph, next_think):
                if pending is not None and not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
            for ev in await thinking_engine.get_queued_events():
                yield ("thinking", ev)

    async def invoke_agent(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
    ) -> AgentExecutionResult:
        """Execute the agent on a specific thread or explicit task with user input."""
        self._verify_event_loop()
        existing_task: AgentTask | None = None
        lock_key = thread_id
        if task_id is not None:
            existing_task = self._validate_task_executable(task_id)
            lock_key = existing_task.thread_id

        task_token = self._begin_runtime_run()
        try:
            async with self._acquire_thread_lock(lock_key):
                if task_id is not None:
                    existing_task = self._validate_task_executable(task_id)
                return await self._invoke_agent_unlocked(
                    lock_key,
                    user_message,
                    run_id=run_id,
                    existing_task=existing_task,
                )
        finally:
            self._end_runtime_run(task_token)

    async def execute_task(
        self,
        task_id: str,
        *,
        user_message: str | None = None,
        run_id: str | None = None,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        checkpoint_id: str | None = None,
        fresh_checkpoint: bool = False,
        allow_queued: bool = False,
        pause_checker: Callable[[], bool] | None = None,
    ) -> AgentExecutionResult:
        """Execute an existing standalone or session-linked Task on its isolated thread_id."""
        self._verify_event_loop()
        task_preview = self._validate_task_executable(task_id, allow_queued=allow_queued)
        task_token = self._begin_runtime_run()
        try:
            async with self._acquire_thread_lock(task_preview.thread_id):
                locked_task = self._validate_task_executable(task_id, allow_queued=allow_queued)
                effective_query = (
                    user_message.strip()
                    if isinstance(user_message, str) and user_message.strip()
                    else locked_task.query
                )
                return await self._invoke_agent_unlocked(
                    locked_task.thread_id,
                    effective_query,
                    run_id=run_id,
                    existing_task=locked_task,
                    trigger_type=trigger_type,
                    checkpoint_id=checkpoint_id,
                    fresh_checkpoint=fresh_checkpoint,
                    pause_checker=pause_checker,
                )
        finally:
            self._end_runtime_run(task_token)

    async def _invoke_agent_unlocked(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
        *,
        existing_task: AgentTask | None = None,
        trigger_type: RunTriggerType = RunTriggerType.INITIAL,
        checkpoint_id: str | None = None,
        fresh_checkpoint: bool = False,
        pause_checker: Callable[[], bool] | None = None,
    ) -> AgentExecutionResult:
        """Internal invoke implementation executed under the per-thread lock."""
        effective_session_id = (
            existing_task.session_id if existing_task is not None else thread_id
        )
        effective_thread_id = (
            existing_task.thread_id if existing_task is not None else thread_id
        )

        if effective_session_id is not None:
            self.session_service.get_or_create_session(effective_session_id)

        effective_run_id = set_trace_context(effective_thread_id, run_id)
        guard = get_route_guard()
        trace_store = self.trace_store or get_trace_store()

        runtime_id = self.hitl_manager.runtime_instance_id or "standalone"
        if existing_task is not None:
            task = existing_task
        else:
            task = self.task_coordinator.create_task(
                session_id=effective_session_id,
                thread_id=effective_thread_id,
                query=user_message,
                runtime_id=runtime_id,
            )

        if fresh_checkpoint:
            cp = getattr(self.graph, "checkpointer", None)
            if cp is not None and callable(getattr(cp, "adelete_thread", None)):
                await cp.adelete_thread(effective_thread_id)

        run = self.task_coordinator.start_run(
            task_id=task.task_id,
            runtime_id=runtime_id,
            trigger_type=trigger_type,
            run_id=effective_run_id,
            checkpoint_id=None if fresh_checkpoint else checkpoint_id,
            enforce_single_flight=(
                existing_task is not None and trigger_type != RunTriggerType.HITL_RESUME
            ),
        )
        self._safe_record_event(
            task.task_id,
            EventType.RUN_STARTED,
            run_id=run.run_id,
            payload={
                "run_seq": run.run_seq,
                "trigger_type": trigger_type.value,
                "runtime_id": runtime_id,
                "checkpoint_id": None if fresh_checkpoint else checkpoint_id,
            },
        )

        trace = ConversationTrace(
            session_id=effective_session_id,
            run_id=effective_run_id,
            task_id=task.task_id,
            query=user_message,
        )
        trace.input_stage = {
            "query": user_message,
            "thread_id": effective_thread_id,
            "session_id": effective_session_id,
            "task_id": task.task_id,
        }

        # 1. Front-line Guard Evaluation (<50ms fast-path rule check for chat sessions)
        is_chat_thread = (
            existing_task is None
            and effective_session_id is not None
            and effective_thread_id == effective_session_id
        )
        guard_start = time.time()
        if is_chat_thread and effective_session_id is not None:
            decision = guard.evaluate(user_message, effective_session_id)
        else:
            decision = GuardDecision(
                action=GuardAction.PASS_THROUGH,
                matched_rule="task_execution_direct" if existing_task is not None else "standalone_task_direct",
            )
        guard_ms = int((time.time() - guard_start) * 1000)
        trace.guard_stage = {
            "action": decision.action.value,
            "rule": decision.matched_rule,
            "duration_ms": guard_ms,
        }

        # Handle Fast-Path Direct Reply or Emergency Halt (chat path)
        if decision.action in (GuardAction.DIRECT_REPLY, GuardAction.HALT_TASK):
            reply_text = decision.reply_text or "已处理。"
            if is_chat_thread and effective_session_id is not None:
                self.session_service.add_message(
                    effective_session_id,
                    role="user",
                    content=user_message,
                    task_id=task.task_id,
                    run_id=run.run_id,
                )
                self.session_service.add_message(
                    effective_session_id,
                    role="assistant",
                    content=reply_text,
                    task_id=task.task_id,
                    run_id=run.run_id,
                )
            self.task_coordinator.update_task_metadata(
                task.task_id, result_summary=reply_text[:500]
            )
            self.task_coordinator.complete_task_and_run(task.task_id, run.run_id)
            self._safe_record_event(
                task.task_id,
                EventType.TASK_COMPLETED,
                run_id=run.run_id,
                payload={"fast_path": True},
            )
            trace.mark_ttft()
            trace.mark_ttfa()
            trace.finish(output=reply_text, status="rule_direct")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()

            return AgentExecutionResult(
                thread_id=effective_thread_id,
                run_id=effective_run_id,
                task_id=task.task_id,
                status="completed",
                output_text=reply_text,
            )

        # Record user message and handle enriched intent (only for Chat turns, not isolated Tasks)
        actual_input_message = decision.enriched_message or user_message
        if is_chat_thread and effective_session_id is not None:
            self.session_service.add_message(
                effective_session_id,
                role="user",
                content=user_message,
                task_id=task.task_id,
                run_id=run.run_id,
            )

        configurable: dict[str, Any] = {"thread_id": effective_thread_id}
        if checkpoint_id and not fresh_checkpoint:
            configurable["checkpoint_id"] = checkpoint_id
        config = {"configurable": configurable}

        # Check if state needs historical context from SQLite (only for Chat threads, never bleed across Tasks)
        state_snapshot = await self.graph.aget_state(config)
        messages_input = [HumanMessage(content=actual_input_message)]
        if is_chat_thread and effective_session_id is not None and not state_snapshot.values.get("messages"):
            past_records = self.session_service.get_messages(effective_session_id)
            if len(past_records) > 1:
                history_msgs = []
                for rec in past_records[:-1]:
                    if rec.role == "user":
                        history_msgs.append(HumanMessage(content=rec.content))
                    elif rec.role == "assistant":
                        history_msgs.append(AIMessage(content=rec.content))
                messages_input = history_msgs + [HumanMessage(content=actual_input_message)]

        trace.preload_stage = {
            "history_loaded_count": len(messages_input) - 1,
            "sticky_context": (
                guard.get_sticky_context(effective_session_id)
                if is_chat_thread and effective_session_id is not None
                else {}
            ),
        }

        input_state = {
            "messages": messages_input,
            "run_context": {
                "thread_id": effective_thread_id,
                "session_id": effective_session_id,
                "run_id": effective_run_id,
                "task_id": task.task_id,
                "knowledge_scope": task.get_knowledge_scope(),
            },
        }
        is_checkpoint_resume = (
            trigger_type == RunTriggerType.RESUME
            and not fresh_checkpoint
            and bool(state_snapshot.next)
            and not any(getattr(t, "interrupts", None) for t in (state_snapshot.tasks or ()))
        )
        graph_input: dict[str, Any] | None = None if is_checkpoint_resume else input_state

        paused_mid_run = False
        final_state: dict[str, Any] = {}
        try:
            with timed_step("agent_run", thread_id=effective_thread_id, run_id=effective_run_id):
                if hasattr(self.graph, "astream"):
                    async for chunk in self.graph.astream(
                        graph_input, config=config, stream_mode="values"
                    ):
                        if isinstance(chunk, dict):
                            final_state = chunk
                        if pause_checker is not None and pause_checker():
                            snap_check = await self.graph.aget_state(config)
                            if snap_check.next:
                                paused_mid_run = True
                                break
                else:
                    final_state = await self.graph.ainvoke(graph_input, config=config)

            # Check if execution paused cooperatively or on an HITL interrupt
            state_snapshot = await self.graph.aget_state(config)
            cp_id = _extract_checkpoint_id(state_snapshot)
            if paused_mid_run:
                self.task_coordinator.pause_task_and_run(
                    task.task_id, run.run_id, checkpoint_id=cp_id
                )
                self._safe_record_event(
                    task.task_id,
                    EventType.TASK_PAUSED,
                    run_id=run.run_id,
                    payload={"reason": "Paused by user", "checkpoint_id": cp_id},
                )
                trace.finish(output="[Paused by user]", status="paused")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()

                return AgentExecutionResult(
                    thread_id=effective_thread_id,
                    run_id=effective_run_id,
                    task_id=task.task_id,
                    status="paused",
                    output_text="",
                )

            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for t in tasks:
                    if t.interrupts:
                        interrupt_val = t.interrupts[0].value
                        break

                self.task_coordinator.pause_for_approval(
                    task.task_id, run.run_id, checkpoint_id=cp_id
                )
                self._safe_record_event(
                    task.task_id,
                    EventType.APPROVAL_REQUIRED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": cp_id},
                )
                self._safe_record_event(
                    task.task_id,
                    EventType.RUN_PAUSED,
                    run_id=run.run_id,
                    payload={"reason": "waiting_approval", "checkpoint_id": cp_id},
                )
                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()

                return AgentExecutionResult(
                    thread_id=effective_thread_id,
                    run_id=effective_run_id,
                    task_id=task.task_id,
                    status="interrupted",
                    pending_approval=interrupt_val,
                )

            # Completed normally
            messages = final_state.get("messages", [])
            output = ""
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.content:
                    output = str(m.content)
                    break

            if output:
                if is_chat_thread and effective_session_id is not None:
                    self.session_service.add_message(
                        effective_session_id,
                        role="assistant",
                        content=output,
                        task_id=task.task_id,
                        run_id=run.run_id,
                    )
                self.task_coordinator.update_task_metadata(
                    task.task_id, result_summary=output[:500]
                )

            self.task_coordinator.complete_task_and_run(
                task.task_id, run.run_id, checkpoint_id=cp_id
            )
            self._safe_record_event(
                task.task_id,
                EventType.TASK_COMPLETED,
                run_id=run.run_id,
                payload={"checkpoint_id": cp_id},
            )
            trace.finish(output=output, status="completed")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()

            return AgentExecutionResult(
                thread_id=effective_thread_id,
                run_id=effective_run_id,
                task_id=task.task_id,
                status="completed",
                output_text=output,
            )

        except asyncio.CancelledError:
            is_unknown = False
            try:
                if (
                    self.hitl_manager
                    and hasattr(self.hitl_manager, "store")
                    and self.hitl_manager.store is not None
                ):
                    unfinalized = self.hitl_manager.store.mark_run_in_flight_outcomes_unknown(run.run_id)
                    if unfinalized > 0:
                        is_unknown = True
            except Exception:
                pass

            self.task_coordinator.cancel_task_and_run(
                task.task_id, run.run_id, is_unknown=is_unknown, error_message="Cancelled"
            )
            self._safe_record_event(
                task.task_id,
                EventType.TASK_CANCELLED,
                run_id=run.run_id,
                payload={"reason": "Cancelled", "is_unknown": is_unknown},
            )
            trace.finish(
                output="",
                error="Cancelled",
                status="unknown" if is_unknown else "cancelled",
            )
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error during agent invocation on thread {effective_thread_id}: {e}")
            self.task_coordinator.fail_task_and_run(task.task_id, run.run_id, error_message=str(e))
            self._safe_record_event(
                task.task_id,
                EventType.RUN_FAILED,
                run_id=run.run_id,
                payload={"error": str(e)},
            )
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            return AgentExecutionResult(
                thread_id=effective_thread_id,
                run_id=effective_run_id,
                task_id=task.task_id,
                status="error",
                error=str(e),
            )

    async def stream_agent(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream token-by-token and tool execution steps from the StateGraph."""
        self._verify_event_loop()
        existing_task: AgentTask | None = None
        lock_key = thread_id
        if task_id is not None:
            existing_task = self._validate_task_executable(task_id)
            lock_key = existing_task.thread_id

        task_token = self._begin_runtime_run()
        try:
            async with self._acquire_thread_lock(lock_key):
                if task_id is not None:
                    existing_task = self._validate_task_executable(task_id)
                inner = self._stream_agent_unlocked(
                    lock_key,
                    user_message,
                    run_id=run_id,
                    existing_task=existing_task,
                )
                try:
                    async for event in inner:
                        yield event
                finally:
                    await inner.aclose()
        finally:
            self._end_runtime_run(task_token)

    async def _stream_agent_unlocked(
        self,
        thread_id: str,
        user_message: str,
        run_id: str | None = None,
        *,
        existing_task: AgentTask | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Internal stream implementation executed under the per-thread lock."""
        effective_session_id = (
            existing_task.session_id if existing_task is not None else thread_id
        )
        effective_thread_id = (
            existing_task.thread_id if existing_task is not None else thread_id
        )
        if effective_session_id is not None:
            self.session_service.get_or_create_session(effective_session_id)

        effective_run_id = set_trace_context(effective_thread_id, run_id)
        guard = get_route_guard()
        trace_store = self.trace_store or get_trace_store()

        runtime_id = self.hitl_manager.runtime_instance_id or "standalone"
        if existing_task is not None:
            task = existing_task
        else:
            task = self.task_coordinator.create_task(
                session_id=effective_session_id,
                thread_id=effective_thread_id,
                query=user_message,
                runtime_id=runtime_id,
            )
        run = self.task_coordinator.start_run(
            task_id=task.task_id,
            runtime_id=runtime_id,
            trigger_type=RunTriggerType.INITIAL,
            run_id=effective_run_id,
        )
        self._safe_record_event(
            task.task_id,
            EventType.RUN_STARTED,
            run_id=run.run_id,
            payload={"run_seq": run.run_seq, "trigger_type": RunTriggerType.INITIAL.value, "runtime_id": runtime_id},
        )

        trace = ConversationTrace(
            session_id=effective_session_id,
            run_id=effective_run_id,
            task_id=task.task_id,
            query=user_message,
        )
        trace.input_stage = {
            "query": user_message,
            "thread_id": effective_thread_id,
            "session_id": effective_session_id,
            "task_id": task.task_id,
        }

        # 1. Front-line Guard Evaluation (<50ms fast-path rule check)
        is_chat_thread = (
            existing_task is None
            and effective_session_id is not None
            and effective_thread_id == effective_session_id
        )
        guard_start = time.time()
        if is_chat_thread and effective_session_id is not None:
            decision = guard.evaluate(user_message, effective_session_id)
        else:
            decision = GuardDecision(
                action=GuardAction.PASS_THROUGH,
                matched_rule="task_execution_direct" if existing_task is not None else "standalone_task_direct",
            )
        guard_ms = int((time.time() - guard_start) * 1000)
        trace.guard_stage = {
            "action": decision.action.value,
            "rule": decision.matched_rule,
            "duration_ms": guard_ms,
        }

        # Fast-Path Rule Direct Reply (0 tokens, <50ms return)
        if decision.action in (GuardAction.DIRECT_REPLY, GuardAction.HALT_TASK):
            reply_text = decision.reply_text or "已处理。"
            if is_chat_thread and effective_session_id is not None:
                self.session_service.add_message(
                    effective_session_id,
                    role="user",
                    content=user_message,
                    task_id=task.task_id,
                    run_id=run.run_id,
                )
                self.session_service.add_message(
                    effective_session_id,
                    role="assistant",
                    content=reply_text,
                    task_id=task.task_id,
                    run_id=run.run_id,
                )
            self.task_coordinator.update_task_metadata(
                task.task_id, result_summary=reply_text[:500]
            )
            self.task_coordinator.complete_task_and_run(task.task_id, run.run_id)
            self._safe_record_event(
                task.task_id,
                EventType.TASK_COMPLETED,
                run_id=run.run_id,
                payload={"fast_path": True},
            )
            trace.mark_ttft()
            trace.mark_ttfa()
            trace.finish(output=reply_text, status="rule_direct")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()

            yield {
                "event": "thinking",
                "data": {
                    "phase": "understand",
                    "status": "completed",
                    "text": "命中规则守卫直出通道 (<50ms)",
                    "elapsed_ms": guard_ms,
                },
            }
            yield {"event": "token", "data": {"token": reply_text}}
            yield {
                "event": "completed",
                "data": {
                    "thread_id": effective_thread_id,
                    "run_id": effective_run_id,
                    "task_id": task.task_id,
                    "output_text": reply_text,
                    "trace_id": trace.trace_id,
                    "fast_path": True,
                },
            }
            return

        # Regular StateGraph execution
        actual_input_message = decision.enriched_message or user_message
        if is_chat_thread and effective_session_id is not None:
            self.session_service.add_message(
                effective_session_id,
                role="user",
                content=user_message,
                task_id=task.task_id,
                run_id=run.run_id,
            )
        config = {"configurable": {"thread_id": effective_thread_id}}

        # Check if state needs historical context from SQLite
        state_snapshot = await self.graph.aget_state(config)
        messages_input = [HumanMessage(content=actual_input_message)]
        if is_chat_thread and effective_session_id is not None and not state_snapshot.values.get("messages"):
            past_records = self.session_service.get_messages(effective_session_id)
            if len(past_records) > 1:
                history_msgs = []
                for rec in past_records[:-1]:
                    if rec.role == "user":
                        history_msgs.append(HumanMessage(content=rec.content))
                    elif rec.role == "assistant":
                        history_msgs.append(AIMessage(content=rec.content))
                messages_input = history_msgs + [HumanMessage(content=actual_input_message)]

        preloaded_text, preloaded_sources = await self._preload_rag_context(
            actual_input_message,
            task.get_knowledge_scope(),
        )

        trace.preload_stage = {
            "history_loaded_count": len(messages_input) - 1,
            "sticky_context": (
                guard.get_sticky_context(effective_session_id)
                if is_chat_thread and effective_session_id is not None
                else {}
            ),
            "preloaded_rag_count": len(preloaded_sources),
            "preloaded_sources": preloaded_sources,
        }

        run_context: dict[str, Any] = {
            "thread_id": effective_thread_id,
            "session_id": effective_session_id,
            "run_id": effective_run_id,
            "task_id": task.task_id,
            "knowledge_scope": task.get_knowledge_scope(),
        }
        if preloaded_text:
            run_context["preloaded_context"] = preloaded_text

        input_state = {
            "messages": messages_input,
            "run_context": run_context,
        }

        thinking_engine = ThinkingEngine(effective_thread_id)
        # Phase 1: Emit immediate understanding event (<100ms)
        await thinking_engine.emit_understand("正在理解您的指令并分析上下文...")
        trace.mark_ttft()

        for ev in await thinking_engine.get_queued_events():
            yield ev

        active_tools: dict[str, tuple[float, Any, str, str | None]] = {}
        parallel_batch_seq = 0
        current_parallel_group: str | None = None
        trace.add_step(
            step_type="understand",
            name="意图分析与上下文检查",
            arguments={"query": user_message},
            result={"action": decision.action.value, "rule_name": decision.matched_rule},
            duration_ms=guard_ms,
        )

        try:
            graph_stream = self.graph.astream_events(
                input_state, config=config, version="v2"
            )
            async for source_kind, item in self._iter_graph_with_thinking(
                graph_stream, thinking_engine
            ):
                if source_kind == "thinking":
                    yield item
                    continue

                event = item
                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        trace.mark_ttfa()
                        thinking_engine.start_heartbeat()
                        yield {"event": "token", "data": {"token": str(chunk.content)}}

                elif kind == "on_tool_start":
                    t_name = str(event.get("name") or "tool")
                    tool_run_id = str(event.get("run_id") or t_name)
                    t_input = event.get("data", {}).get("input")
                    if len(active_tools) == 0:
                        parallel_batch_seq += 1
                        current_parallel_group = None
                    else:
                        current_parallel_group = f"parallel_{parallel_batch_seq}"
                        # Retroactively tag already-active tools in this concurrent batch
                        for k, (st, args, nm, _) in list(active_tools.items()):
                            active_tools[k] = (st, args, nm, current_parallel_group)
                    active_tools[tool_run_id] = (
                        time.time(),
                        t_input,
                        t_name,
                        current_parallel_group,
                    )
                    self.task_coordinator.set_task_status(task.task_id, TaskStatus.EXECUTING_TOOL)
                    self._safe_record_event(
                        task.task_id,
                        EventType.TOOL_STARTED,
                        run_id=run.run_id,
                        payload={"tool": t_name},
                    )
                    await thinking_engine.emit_tool_start(t_name, t_input)
                    for ev in await thinking_engine.get_queued_events():
                        yield ev
                    yield {
                        "event": "tool_start",
                        "data": {
                            "tool": t_name,
                            "input": t_input,
                        },
                    }

                elif kind == "on_tool_end":
                    t_name = str(event.get("name") or "tool")
                    tool_run_id = str(event.get("run_id") or t_name)
                    raw_out = event.get("data", {}).get("output")
                    out_str = str(raw_out)[:2000] if raw_out is not None else ""
                    start_t, t_args, _, group_label = active_tools.pop(
                        tool_run_id, (time.time(), None, t_name, None)
                    )
                    t_duration_ms = int((time.time() - start_t) * 1000)
                    if not active_tools:
                        self.task_coordinator.set_task_status(task.task_id, TaskStatus.RUNNING)
                    self._safe_record_event(
                        task.task_id,
                        EventType.TOOL_COMPLETED,
                        run_id=run.run_id,
                        payload={"tool": t_name, "duration_ms": t_duration_ms},
                    )

                    trace.add_step(
                        step_type=self._resolve_tool_step_type(t_name),
                        name=t_name,
                        arguments=t_args,
                        result=out_str[:500],
                        duration_ms=t_duration_ms,
                        group=group_label,
                    )
                    await thinking_engine.emit_tool_end(t_name, out_str)
                    for ev in await thinking_engine.get_queued_events():
                        yield ev
                    yield {
                        "event": "tool_end",
                        "data": {
                            "tool": t_name,
                            "output": out_str,
                            "duration_ms": t_duration_ms,
                        },
                    }

            # Phase 4: Finalize thinking
            trace.add_step(
                step_type="organize",
                name="组织输出并交付",
                duration_ms=0,
            )
            await thinking_engine.emit_completed()
            for ev in await thinking_engine.get_queued_events():
                yield ev
            thinking_engine.close()

            state_snapshot = await self.graph.aget_state(config)
            checkpoint_id = _extract_checkpoint_id(state_snapshot)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for t in tasks:
                    if t.interrupts:
                        interrupt_val = t.interrupts[0].value
                        break
                self.task_coordinator.pause_for_approval(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
                self._safe_record_event(
                    task.task_id,
                    EventType.APPROVAL_REQUIRED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": checkpoint_id},
                )
                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()
                yield {
                    "event": "approval_required",
                    "data": interrupt_val or {"status": "pending_approval"},
                }
            else:
                messages = state_snapshot.values.get("messages", [])
                output = ""
                for m in reversed(messages):
                    if isinstance(m, AIMessage) and m.content:
                        output = str(m.content)
                        break
                if output:
                    if effective_session_id is not None and task.thread_id == effective_session_id:
                        self.session_service.add_message(
                            effective_session_id,
                            role="assistant",
                            content=output,
                            task_id=task.task_id,
                            run_id=run.run_id,
                        )
                    self.task_coordinator.update_task_metadata(
                        task.task_id, result_summary=output[:500]
                    )

                self.task_coordinator.complete_task_and_run(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
                self._safe_record_event(
                    task.task_id,
                    EventType.TASK_COMPLETED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": checkpoint_id},
                )
                trace.finish(output=output, status="completed")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()

                yield {
                    "event": "completed",
                    "data": {
                        "thread_id": effective_thread_id,
                        "run_id": effective_run_id,
                        "task_id": task.task_id,
                        "output_text": output,
                        "trace_id": trace.trace_id,
                        "ttft_ms": trace.ttft_ms,
                        "ttfa_ms": trace.ttfa_ms,
                        "ttlt_ms": trace.ttlt_ms,
                    },
                }

        except (asyncio.CancelledError, GeneratorExit):
            thinking_engine.close()
            is_unknown = len(active_tools) > 0
            self.task_coordinator.cancel_task_and_run(
                task.task_id,
                run.run_id,
                is_unknown=is_unknown,
                error_message="Client disconnected during streaming execution",
            )
            self._safe_record_event(
                task.task_id,
                EventType.TASK_CANCELLED,
                run_id=run.run_id,
                payload={"is_unknown": is_unknown},
            )
            trace.finish(
                output="",
                error="Client disconnected during streaming execution",
                status="unknown" if is_unknown else "cancelled",
            )
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            raise
        except Exception as e:  # noqa: BLE001
            thinking_engine.close()
            logger.error(f"Error during streaming execution on thread {effective_thread_id}: {e}")
            self.task_coordinator.fail_task_and_run(task.task_id, run.run_id, error_message=str(e))
            self._safe_record_event(
                task.task_id,
                EventType.RUN_FAILED,
                run_id=run.run_id,
                payload={"error": str(e)},
            )
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            yield {"event": "error", "data": {"error": str(e), "trace_id": trace.trace_id}}

    async def verify_approval_context(self, thread_id: str, approval_id: str) -> bool:
        """Verify server-side binding between thread_id, approval_id, and active interrupt."""
        self._verify_event_loop()
        task_token = self._begin_runtime_run()
        try:
            if (
                not isinstance(thread_id, str)
                or not thread_id.strip()
                or thread_id.strip().lower() == "default"
            ):
                return False

            clean_tid = thread_id.strip()
            async with self._acquire_thread_lock(clean_tid):
                return await self._verify_approval_context_unlocked(clean_tid, approval_id)
        finally:
            self._end_runtime_run(task_token)

    async def _verify_approval_context_unlocked(self, thread_id: str, approval_id: str) -> bool:
        """Internal approval context check assumed to hold the per-thread lock."""
        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or thread_id.strip().lower() == "default"
        ):
            return False

        clean_tid = thread_id.strip()
        if (
            self.session_service.get_session(clean_tid) is None
            and self.task_coordinator.get_task_by_thread_id(clean_tid) is None
        ):
            return False

        if (
            not isinstance(approval_id, str)
            or not approval_id.strip()
        ):
            return False

        clean_appr_id = approval_id.strip()
        req = self.hitl_manager.get_request(clean_appr_id, thread_id=clean_tid)
        if not req:
            return False
        is_pending = req.status == ApprovalStatus.PENDING
        is_resumable_unleased = (
            req.status in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED)
            and req.lease_owner_instance_id is None
        )
        if not (is_pending or is_resumable_unleased):
            return False

        try:
            state_snapshot = await self.graph.aget_state(
                {"configurable": {"thread_id": clean_tid}}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Checkpoint lookup failed during approval verification for '{clean_tid}': {exc}"
            )
            return False

        if not state_snapshot or not state_snapshot.next:
            return False

        run_ctx = (state_snapshot.values or {}).get("run_context") or {}
        if not isinstance(run_ctx, dict):
            return False
        ctx_tid = run_ctx.get("thread_id")
        if ctx_tid is None:
            ctx_tid = run_ctx.get("session_id")
        if ctx_tid != clean_tid:
            return False

        for task in state_snapshot.tasks or ():
            for intr in getattr(task, "interrupts", ()) or ():
                val = getattr(intr, "value", None)
                if not isinstance(val, dict):
                    continue
                if val.get("thread_id") != clean_tid:
                    continue
                if val.get("approval_id") == clean_appr_id:
                    return True
                for sub in val.get("requests") or []:
                    if (
                        isinstance(sub, dict)
                        and sub.get("approval_id") == clean_appr_id
                        and sub.get("thread_id") == clean_tid
                    ):
                        return True

        return False

    def _prepare_persisted_resume_decision(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> str | None:
        """Enforce Scenario C rules when resuming a previously persisted APPROVED/MODIFIED request."""
        req = self.hitl_manager.get_request(approval_id, thread_id=thread_id)
        if req is None:
            return "Approval request not found"
        if req.status not in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED):
            return None

        submitted = decision.decision.lower()
        if submitted == "reject":
            self.hitl_manager.abort_approved_request(
                approval_id, thread_id=thread_id, reason=decision.reason
            )
            return None

        persisted_dec = (req.decision or "").lower()
        if submitted != persisted_dec:
            return (
                f"Cannot overwrite persisted approval decision '{persisted_dec}' "
                f"with '{submitted}'"
            )
        if (
            req.status == ApprovalStatus.MODIFIED
            and decision.modified_arguments is not None
            and compute_arguments_hash(decision.modified_arguments)
            != compute_arguments_hash(req.modified_arguments)
        ):
            return "Cannot overwrite persisted modified arguments"
        return None

    async def resume_task_approval(
        self,
        task_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AgentExecutionResult:
        """Resume an interrupted Task by approval decision using the task's isolated thread_id."""
        task = self.task_coordinator.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.archived_at is not None:
            raise ValueError(f"Task '{task_id}' is archived and cannot be resumed")
        return await self.resume_approval(
            thread_id=task.thread_id,
            approval_id=approval_id,
            decision=decision,
        )

    async def resume_approval(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AgentExecutionResult:
        """Resume an interrupted agent run by submitting an approval decision."""
        self._verify_event_loop()
        task_token = self._begin_runtime_run()
        try:
            async with self._acquire_thread_lock(thread_id):
                return await self._resume_approval_unlocked(thread_id, approval_id, decision)
        finally:
            self._end_runtime_run(task_token)

    async def _resume_approval_unlocked(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AgentExecutionResult:
        """Internal approval resume executed under the per-thread lock."""
        if not await self._verify_approval_context_unlocked(thread_id, approval_id):
            logger.warning(
                f"Blocked resume_approval due to invalid thread/approval binding: thread='{thread_id}', approval='{approval_id}'"
            )
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id="",
                status="error",
                error="Approval request not found",
            )

        prep_err = self._prepare_persisted_resume_decision(thread_id, approval_id, decision)
        if prep_err is not None:
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id="",
                status="error",
                error=prep_err,
            )

        effective_run_id = set_trace_context(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        trace_store = self.trace_store or get_trace_store()
        runtime_id = self.hitl_manager.runtime_instance_id or "standalone"

        task = self.task_coordinator.get_task_by_approval(approval_id)
        if task is None:
            task = self.task_coordinator.get_active_task_for_thread(thread_id)
        if task is None:
            task = self.task_coordinator.get_task_by_thread_id(thread_id)
        if task is None:
            task = self.task_coordinator.create_task(
                session_id=thread_id,
                thread_id=thread_id,
                query=f"Resume approval {approval_id}",
                runtime_id=runtime_id,
            )
        effective_session_id = task.session_id

        run = self.task_coordinator.start_run(
            task_id=task.task_id,
            runtime_id=runtime_id,
            trigger_type=RunTriggerType.HITL_RESUME,
            run_id=effective_run_id,
        )
        self._safe_record_event(
            task.task_id,
            EventType.APPROVAL_RESOLVED,
            run_id=run.run_id,
            payload={"approval_id": approval_id, "decision": decision.decision},
        )
        self._safe_record_event(
            task.task_id,
            EventType.RUN_RESUMED,
            run_id=run.run_id,
            payload={"run_seq": run.run_seq, "trigger_type": RunTriggerType.HITL_RESUME.value},
        )

        trace = ConversationTrace(
            session_id=effective_session_id,
            run_id=effective_run_id,
            task_id=task.task_id,
            query=f"[Resume Approval: {approval_id}]",
        )
        trace.input_stage = {
            "query": f"[Resume Approval: {approval_id}]",
            "thread_id": thread_id,
            "session_id": effective_session_id,
            "task_id": task.task_id,
            "decision": decision.decision,
        }

        logger.info(
            f"Resuming interrupted execution on thread '{thread_id}' with decision '{decision.decision}' "
            f"(task_id={task.task_id}, run_id={run.run_id})"
        )

        try:
            resume_payload = {
                "approval_id": approval_id,
                "decision": decision.decision,
                "reason": decision.reason,
                "modified_arguments": decision.modified_arguments,
            }
            final_state = await self.graph.ainvoke(
                Command(resume=resume_payload),
                config=config,
            )

            state_snapshot = await self.graph.aget_state(config)
            checkpoint_id = _extract_checkpoint_id(state_snapshot)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for t in tasks:
                    if t.interrupts:
                        interrupt_val = t.interrupts[0].value
                        break
                self.task_coordinator.pause_for_approval(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
                self._safe_record_event(
                    task.task_id,
                    EventType.APPROVAL_REQUIRED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": checkpoint_id},
                )
                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()
                return AgentExecutionResult(
                    thread_id=thread_id,
                    run_id=effective_run_id,
                    task_id=task.task_id,
                    status="interrupted",
                    pending_approval=interrupt_val,
                )

            messages = final_state.get("messages", [])
            output = ""
            for m in reversed(messages):
                if isinstance(m, AIMessage) and m.content:
                    output = str(m.content)
                    break

            if output:
                if effective_session_id is not None and task.thread_id == effective_session_id:
                    self.session_service.add_message(
                        effective_session_id,
                        role="assistant",
                        content=output,
                        task_id=task.task_id,
                        run_id=run.run_id,
                    )
                self.task_coordinator.update_task_metadata(
                    task.task_id, result_summary=output[:500]
                )

            self.task_coordinator.complete_task_and_run(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
            self._safe_record_event(
                task.task_id,
                EventType.TASK_COMPLETED,
                run_id=run.run_id,
                payload={"checkpoint_id": checkpoint_id},
            )
            trace.finish(output=output, status="completed")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()

            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                task_id=task.task_id,
                status="completed",
                output_text=output,
            )
        except asyncio.CancelledError:
            is_unknown = False
            try:
                if (
                    self.hitl_manager
                    and hasattr(self.hitl_manager, "store")
                    and self.hitl_manager.store is not None
                ):
                    unfinalized = self.hitl_manager.store.mark_run_in_flight_outcomes_unknown(run.run_id)
                    if unfinalized > 0:
                        is_unknown = True
            except Exception:
                pass

            self.task_coordinator.cancel_task_and_run(
                task.task_id, run.run_id, is_unknown=is_unknown, error_message="Cancelled"
            )
            self._safe_record_event(
                task.task_id,
                EventType.TASK_CANCELLED,
                run_id=run.run_id,
                payload={"reason": "Cancelled", "is_unknown": is_unknown},
            )
            trace.finish(
                output="",
                error="Cancelled",
                status="unknown" if is_unknown else "cancelled",
            )
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to resume thread {thread_id}: {e}")
            self.task_coordinator.fail_task_and_run(task.task_id, run.run_id, error_message=str(e))
            self._safe_record_event(
                task.task_id,
                EventType.RUN_FAILED,
                run_id=run.run_id,
                payload={"error": str(e)},
            )
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            return AgentExecutionResult(
                thread_id=thread_id,
                run_id=effective_run_id,
                task_id=task.task_id,
                status="error",
                error=str(e),
            )

    async def stream_resume_approval(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Resume an interrupted agent run with real-time SSE event streaming."""
        self._verify_event_loop()
        task_token = self._begin_runtime_run()
        try:
            async with self._acquire_thread_lock(thread_id):
                inner = self._stream_resume_approval_unlocked(thread_id, approval_id, decision)
                try:
                    async for event in inner:
                        yield event
                finally:
                    await inner.aclose()
        finally:
            self._end_runtime_run(task_token)

    async def _stream_resume_approval_unlocked(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Internal streaming approval resume executed under the per-thread lock."""
        if not await self._verify_approval_context_unlocked(thread_id, approval_id):
            logger.warning(
                f"Blocked stream_resume_approval due to invalid thread/approval binding: thread='{thread_id}', approval='{approval_id}'"
            )
            yield {"event": "error", "data": {"error": "Approval request not found"}}
            return

        prep_err = self._prepare_persisted_resume_decision(thread_id, approval_id, decision)
        if prep_err is not None:
            yield {"event": "error", "data": {"error": prep_err}}
            return

        effective_run_id = set_trace_context(thread_id)
        config = {"configurable": {"thread_id": thread_id}}
        trace_store = self.trace_store or get_trace_store()
        runtime_id = self.hitl_manager.runtime_instance_id or "standalone"

        task = self.task_coordinator.get_task_by_approval(approval_id)
        if task is None:
            task = self.task_coordinator.get_active_task_for_thread(thread_id)
        if task is None:
            task = self.task_coordinator.get_task_by_thread_id(thread_id)
        if task is None:
            task = self.task_coordinator.create_task(
                session_id=thread_id,
                thread_id=thread_id,
                query=f"Resume approval {approval_id}",
                runtime_id=runtime_id,
            )
        effective_session_id = task.session_id

        run = self.task_coordinator.start_run(
            task_id=task.task_id,
            runtime_id=runtime_id,
            trigger_type=RunTriggerType.HITL_RESUME,
            run_id=effective_run_id,
        )
        self._safe_record_event(
            task.task_id,
            EventType.APPROVAL_RESOLVED,
            run_id=run.run_id,
            payload={"approval_id": approval_id, "decision": decision.decision},
        )
        self._safe_record_event(
            task.task_id,
            EventType.RUN_RESUMED,
            run_id=run.run_id,
            payload={"run_seq": run.run_seq, "trigger_type": RunTriggerType.HITL_RESUME.value},
        )

        trace = ConversationTrace(
            session_id=effective_session_id,
            run_id=effective_run_id,
            task_id=task.task_id,
            query=f"[Resume Approval: {approval_id}]",
        )
        trace.input_stage = {
            "query": f"[Resume Approval: {approval_id}]",
            "thread_id": thread_id,
            "session_id": effective_session_id,
            "task_id": task.task_id,
            "decision": decision.decision,
        }

        resume_payload = {
            "approval_id": approval_id,
            "decision": decision.decision,
            "reason": decision.reason,
            "modified_arguments": decision.modified_arguments,
        }

        active_tools: dict[str, tuple[float, Any, str, str | None]] = {}
        parallel_batch_seq = 0
        current_parallel_group: str | None = None
        try:
            async for event in self.graph.astream_events(
                Command(resume=resume_payload), config=config, version="v2"
            ):
                kind = event.get("event")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        trace.mark_ttfa()
                        yield {"event": "token", "data": {"token": str(chunk.content)}}
                elif kind == "on_tool_start":
                    t_name = str(event.get("name") or "tool")
                    tool_run_id = str(event.get("run_id") or t_name)
                    t_input = event.get("data", {}).get("input")
                    if len(active_tools) == 0:
                        parallel_batch_seq += 1
                        current_parallel_group = None
                    else:
                        current_parallel_group = f"parallel_{parallel_batch_seq}"
                        for k, (st, args, nm, _) in list(active_tools.items()):
                            active_tools[k] = (st, args, nm, current_parallel_group)
                    active_tools[tool_run_id] = (
                        time.time(),
                        t_input,
                        t_name,
                        current_parallel_group,
                    )
                    self.task_coordinator.set_task_status(task.task_id, TaskStatus.EXECUTING_TOOL)
                    yield {
                        "event": "tool_start",
                        "data": {
                            "tool": t_name,
                            "input": t_input,
                        },
                    }
                elif kind == "on_tool_end":
                    t_name = str(event.get("name") or "tool")
                    tool_run_id = str(event.get("run_id") or t_name)
                    raw_out = event.get("data", {}).get("output")
                    out_str = str(raw_out)[:2000] if raw_out is not None else ""
                    start_t, t_args, _, group_label = active_tools.pop(
                        tool_run_id, (time.time(), None, t_name, None)
                    )
                    t_duration_ms = int((time.time() - start_t) * 1000)
                    if not active_tools:
                        self.task_coordinator.set_task_status(task.task_id, TaskStatus.RUNNING)

                    step_args = (
                        decision.modified_arguments
                        if decision.decision == "modify"
                        and decision.modified_arguments is not None
                        else t_args
                    )
                    trace.add_step(
                        step_type=self._resolve_tool_step_type(t_name),
                        name=t_name,
                        arguments=step_args,
                        result=out_str[:500],
                        duration_ms=t_duration_ms,
                        group=group_label,
                    )
                    yield {
                        "event": "tool_end",
                        "data": {
                            "tool": t_name,
                            "output": out_str,
                            "duration_ms": t_duration_ms,
                        },
                    }

            state_snapshot = await self.graph.aget_state(config)
            checkpoint_id = _extract_checkpoint_id(state_snapshot)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = None
                for t in tasks:
                    if t.interrupts:
                        interrupt_val = t.interrupts[0].value
                        break
                self.task_coordinator.pause_for_approval(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
                self._safe_record_event(
                    task.task_id,
                    EventType.APPROVAL_REQUIRED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": checkpoint_id},
                )
                trace.finish(output="[Pending Approval]", status="interrupted")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()
                yield {
                    "event": "approval_required",
                    "data": interrupt_val or {"status": "pending_approval"},
                }
            else:
                messages = state_snapshot.values.get("messages", [])
                output = ""
                for m in reversed(messages):
                    if isinstance(m, AIMessage) and m.content:
                        output = str(m.content)
                        break
                if output:
                    if effective_session_id is not None and task.thread_id == effective_session_id:
                        self.session_service.add_message(
                            effective_session_id,
                            role="assistant",
                            content=output,
                            task_id=task.task_id,
                            run_id=run.run_id,
                        )
                    self.task_coordinator.update_task_metadata(
                        task.task_id, result_summary=output[:500]
                    )

                self.task_coordinator.complete_task_and_run(task.task_id, run.run_id, checkpoint_id=checkpoint_id)
                self._safe_record_event(
                    task.task_id,
                    EventType.TASK_COMPLETED,
                    run_id=run.run_id,
                    payload={"checkpoint_id": checkpoint_id},
                )
                trace.finish(output=output, status="completed")
                trace_store.save_trace(trace)
                trace_store.maybe_prune_old_traces()
                yield {
                    "event": "completed",
                    "data": {
                        "thread_id": thread_id,
                        "run_id": effective_run_id,
                        "task_id": task.task_id,
                        "output_text": output,
                        "trace_id": trace.trace_id,
                    },
                }
        except (asyncio.CancelledError, GeneratorExit):
            is_unknown = len(active_tools) > 0
            self.task_coordinator.cancel_task_and_run(
                task.task_id,
                run.run_id,
                is_unknown=is_unknown,
                error_message="Client disconnected during streaming resume",
            )
            self._safe_record_event(
                task.task_id,
                EventType.TASK_CANCELLED,
                run_id=run.run_id,
                payload={"is_unknown": is_unknown},
            )
            trace.finish(
                output="",
                error="Client disconnected during streaming resume",
                status="unknown" if is_unknown else "cancelled",
            )
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error during streaming resume on thread {thread_id}: {e}")
            self.task_coordinator.fail_task_and_run(task.task_id, run.run_id, error_message=str(e))
            self._safe_record_event(
                task.task_id,
                EventType.RUN_FAILED,
                run_id=run.run_id,
                payload={"error": str(e)},
            )
            trace.finish(output="", error=str(e), status="error")
            trace_store.save_trace(trace)
            trace_store.maybe_prune_old_traces()
            yield {"event": "error", "data": {"error": str(e), "trace_id": trace.trace_id}}


_global_agent_service: AgentService | None = None
_default_agent_service: AgentService | None = None


def get_agent_service() -> AgentService:
    """Retrieve explicit test override, active AgentRuntime service, or fallback singleton."""
    global _default_agent_service
    if _global_agent_service is not None:
        return _global_agent_service

    from jarvis_studio.core.runtime.agent_runtime import get_active_runtime

    active_runtime = get_active_runtime()
    if active_runtime is not None:
        return active_runtime.agent_service

    if _default_agent_service is None:
        _default_agent_service = AgentService()
    return _default_agent_service
