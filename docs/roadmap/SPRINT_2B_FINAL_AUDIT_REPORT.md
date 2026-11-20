# AgentStudio Sprint 2B — Independent Final Architecture Audit Report

**Audit Baseline**: `5bcc479` (after commits `96aa893`, `b50cbdb`, `06bfb88`, `d9c0446`, `cae12c3`, `5bcc479`)  
**Audit Date**: 2026-11-20  
**Final Regression**: **265 passed, 12 skipped (POSIX-only on Windows), 0 failed** across 277 collected items.

---

## 1. Executive Verdict

During this independent code-level audit, **4 genuine P1/P2 defects** were identified by directly inspecting `ExecutionEngine`, `AgentService`, `TaskCoordinator`, `TaskService`, `tasks.py`, and `checkpoint_gc.py`. All 4 P1/P2 defects have been **fixed, verified with dedicated fault-injection and concurrency tests**, and confirmed green under full regression.

No unresolved P0/P1/P2 defects remain. **Sprint 2B is recommended for formal closure.**

---

## 2. Detailed Audit Findings & Fixes

### Area 1 — ExecutionEngine: Queue Persistence & Task Claiming

#### Source Code Inspection
- `ExecutionEngine._queue` (`src/jarvis_studio/core/runtime/execution_engine.py:63`) is an in-memory `asyncio.Queue[ExecutionRequest]`.
- `ExecutionEngine.start()` (`execution_engine.py:87-97`) spawns `_worker_loop` tasks that consume strictly from `self._queue.get()` (`execution_engine.py:407`); it does **not** scan SQLite for `QUEUED` tasks.
- Under `AgentRuntime.start()`, `reconcile_on_startup()` (`src/jarvis_studio/core/persistence/recovery.py:175`) invokes `TaskCoordinator.reconcile_stale_tasks_and_runs()` (`src/jarvis_studio/core/tasks/coordinator.py:983-1030`), which transitions orphaned `QUEUED`, `RUNNING`, and `CANCELLING` tasks/runs from prior processes to `INTERRUPTED` (or `UNKNOWN` if associated with an `UNKNOWN` tool outcome/approval).
- **Persistence Semantics Distinction**:
  1. **Persisted task record**: `agent_tasks` and `agent_runs` are durably persisted in SQLite.
  2. **Persisted scheduling intent**: `queue_run()` persists `QUEUED` status in SQLite, **but the execution queue itself is volatile in-memory**.
  3. **Recovery semantics**: Upon crash/restart under `AgentRuntime` (protected by `RuntimeInstanceLock`), orphaned `QUEUED` tasks converge to `INTERRUPTED` and **require explicit user `resume_task`** rather than automatic background re-execution.

#### Defect Found & Fixed
- **Finding 1.1 [P2] — Single-Flight Bypass & Lack of Atomic DB Guard**:
  - *Location*: `src/jarvis_studio/api/routes/tasks.py:209-231`, `src/jarvis_studio/core/tasks/coordinator.py:239-395`, `src/jarvis_studio/service/agent_service.py:428`.
  - *Trigger*: Sync `POST /api/v1/tasks/{id}/execute?background=false` (`AgentService.execute_task`) bypasses `ExecutionEngine._lock`, while `TaskCoordinator.queue_run()` and `start_run()` previously executed unconditional `INSERT`/`UPDATE` without checking `agent_tasks.status` inside the SQLite `BEGIN IMMEDIATE` transaction. Concurrent sync and background execute requests could both pass initial validation before either updated SQLite.
  - *Fix*:
    - Added an atomic DB status check inside `TaskCoordinator.queue_run()` (`coordinator.py:249-280`) inside `with self._tx() as conn:`.
    - Added `enforce_single_flight: bool = False` to `TaskCoordinator.start_run()` (`coordinator.py:322-395`), enabled by `AgentService._invoke_agent_unlocked()` (`agent_service.py:434`) for non-HITL-resume executions.
  - *Verification*: `tests/test_sprint2b_audit_fixes.py::test_concurrent_single_flight_and_purge_active_guard`.

---

### Area 2 — Pause / Resume: Real Runtime Semantics

#### Defects Found & Fixed
- **Finding 2.1 [P1] — `pause_task` Abused `handle.cancel()`, Corrupting `UNKNOWN` Invariants and Killing In-Flight Tools**:
  - *Location (before fix)*: `execution_engine.py:323-329, 437-447`, `agent_service.py:653-683`.
  - *Trigger & Impact*: Calling `pause_task()` on a running task previously called `handle.cancel()`, injecting `asyncio.CancelledError` into `AgentService._invoke_agent_unlocked`. `AgentService` recorded a false `TASK_CANCELLED` event and marked any in-flight tool outcome as `ExecutionOutcomeStatus.UNKNOWN` (`executor.py:253-319`, `agent_service.py:661-675`), after which `_worker_loop` caught `CancelledError` and overwrote `TaskStatus.UNKNOWN` back to `PAUSED`, violating Sprint 1C `UNKNOWN` non-replay invariants.
  - *Fix*:
    - Replaced `handle.cancel()` in `ExecutionEngine.pause_task()` (`execution_engine.py:323-333`) with cooperative signaling (`req.is_pause = True`), and rejected invalid states (`WAITING_APPROVAL`, `COMPLETED`, `UNKNOWN`, etc.).
    - Updated `AgentService._invoke_agent_unlocked()` (`agent_service.py:582-621`) to iterate `self.graph.astream(..., stream_mode="values")` (falling back to `ainvoke` for minimal test doubles without `astream`). At every superstep boundary, if `pause_checker()` is true and `state_snapshot.next` is non-empty, execution exits cleanly **after the current node finishes and LangGraph commits its superstep checkpoint**, transitions DB to `PAUSED`, and records `EventType.TASK_PAUSED` without raising `CancelledError` or emitting `TASK_CANCELLED`.
    - Updated `ExecutionEngine.cancel_task()` (`execution_engine.py:271`) to clear `req.is_pause = False` so explicit cancellation takes precedence over pending pause.
- **Finding 2.2 [P1] — `resume_task` Re-Injected Initial `HumanMessage` Instead of Resuming from Paused Checkpoint**:
  - *Location (before fix)*: `agent_service.py:366-370, 538-574`.
  - *Trigger & Impact*: `ExecutionEngine.resume_task()` submits `RunTriggerType.RESUME` with `user_message=None`. `AgentService` previously fell back to `locked_task.query` and passed `input_state = {"messages": [HumanMessage(...)]}` to LangGraph, restarting a new turn instead of resuming from `state_snapshot.next`.
  - *Fix*: Added `is_checkpoint_resume` detection in `AgentService._invoke_agent_unlocked()` (`agent_service.py:571-577`): when `trigger_type == RunTriggerType.RESUME`, `state_snapshot.next` is non-empty, and no HITL `__interrupt__` is pending, `graph_input = None` is passed to `self.graph.astream(None, config=config)`, resuming directly from the checkpointed superstep without re-running completed tools or duplicating `HumanMessage`.
  - *Verification*: `tests/test_sprint2b_audit_fixes.py::test_real_langgraph_multistep_pause_and_resume` (verified against real `StateGraph` + `AsyncSqliteSaver`: `step_one_tool` runs once, `pause_task` halts before `step_two_tool`, `resume_task` executes `step_two_tool` once without re-running `step_one_tool`).

---

### Area 3 — SSE: Replay / Live Handoff Correctness

#### Source Code Inspection & Defect Fixed
- **Handoff Ordering**: `stream_task_events()` (`src/jarvis_studio/api/routes/tasks.py:325`) subscribes to `EventBroadcaster.subscribe(task_id)` **before** querying SQLite for historical events, and deduplicates by `seq > last_seq`. On client disconnect (`request.is_disconnected()` or stream termination), the `async with broadcaster.subscribe(task_id)` context manager removes the subscriber queue (`event_broadcaster.py:48-53`).
- **Finding 3.1 [P2] — Unpaginated Replay Truncation, Cross-Thread Broadcast Reordering Drop, and Missing Terminal States**:
  - *Location*: `src/jarvis_studio/api/routes/tasks.py:327-368`, `src/jarvis_studio/core/tasks/coordinator.py:843-873`.
  - *Trigger & Impact*:
    1. Historical replay used a single `limit=1000` query, dropping events >1000 before live streaming.
    2. `TaskCoordinator.record_event()` previously released `self._tx()` before calling `get_event_broadcaster().broadcast(record)`. Concurrent calls across threads/coroutines could commit `seq=N` then `seq=N+1`, broadcast `N+1` first, advance `last_seq = N+1`, and silently drop `N`.
    3. Terminal check at `tasks.py:342` omitted `TaskStatus.UNKNOWN` and `TaskStatus.TIMEOUT`, causing streams on `UNKNOWN`/`TIMEOUT` tasks to hang.
  - *Fix*:
    - Added paginated replay (`batch_limit=500` loop) in `stream_task_events()` (`tasks.py:327-340`).
    - Added `TaskStatus.UNKNOWN` and `TaskStatus.TIMEOUT` to the already-terminal check (`tasks.py:346-352`).
    - Added a **gap-catchup safety net** in the live loop (`tasks.py:368-385`): if `record.seq > last_seq + 1`, missing events are fetched from SQLite and emitted in strict `seq ASC` order.
    - Serialized DB commit + `broadcast()` in `TaskCoordinator.record_event()` under `self._event_lock` (`coordinator.py:869-898`).
  - *Verification*: `tests/test_sprint2b_audit_fixes.py::test_sse_replay_live_handoff_concurrent_events_no_gap_no_dup` (concurrent `asyncio.to_thread` + async emitters producing `seq` 1..25 verified with zero gaps, zero duplicates, and 0 remaining subscribers).

---

### Area 4 — Checkpoint GC & Single-Flight

#### Source Code Inspection & Defect Fixed
- **GC Table Coverage**: `clean_dangling_checkpoints()` and `purge_task_checkpoint_sync()` (`src/jarvis_studio/core/persistence/checkpoint_gc.py:48-55, 162-168`) clean `checkpoints`, `writes`, `checkpoint_blobs`, and `checkpoint_writes`.
- **Active Task & Session Protection**: `clean_dangling_checkpoints()` (`checkpoint_gc.py:127-150`) computes `valid_threads = agent_tasks.thread_id | chat_sessions.session_id` (including archived tasks until permanent purge) and deletes only `all_threads - valid_threads`.
- **Finding 4.1 [P1] — Purge / Archive vs Active `QUEUED` / `CANCELLING` Worker Race**:
  - *Location*: `src/jarvis_studio/service/task_service.py:239-243, 281-285`, `src/jarvis_studio/core/tasks/coordinator.py:799-802`.
  - *Trigger & Impact*: `TaskService.purge_task()` and `archive_task()` did not block `TaskStatus.QUEUED` or `TaskStatus.CANCELLING`, and `TaskCoordinator.purge_task()` did not verify `agent_tasks.status` inside `with self._tx() as conn:`. A concurrent `purge_task()` could delete a `QUEUED` or `CANCELLING` task, its deliverables, and its checkpoints while a worker was starting or cleaning up.
  - *Fix*:
    - Blocked `TaskStatus.QUEUED` and `TaskStatus.CANCELLING` in `TaskService.archive_task()` (`task_service.py:239-245`) and `TaskService.purge_task()` (`task_service.py:281-287`).
    - Added atomic DB-transaction active-state guard (`QUEUED`, `RUNNING`, `EXECUTING_TOOL`, `WAITING_APPROVAL`, `CANCELLING`) inside `TaskCoordinator.purge_task()` (`coordinator.py:818-831`).
  - *Verification*: `tests/test_sprint2b_audit_fixes.py::test_concurrent_single_flight_and_purge_active_guard` and `test_checkpoint_gc_cleans_all_langgraph_tables`.

---

## 3. Known Boundaries (Explicitly Documented)

1. **Single-Process Deployment Boundary**: Multi-process / distributed coordination relies on `AgentRuntime`'s `RuntimeInstanceLock` (exclusive OS file lock on the SQLite persistence root). `ExecutionEngine` is strictly an in-process worker pool; running multiple uncoordinated processes without `AgentRuntime` / `RuntimeInstanceLock` against the same DB is unsupported.
2. **Manual Resume of Crashed `QUEUED` Tasks**: By design, `ExecutionEngine._queue` is volatile in-memory; on process restart under `AgentRuntime`, unstarted `QUEUED` tasks are reconciled to `INTERRUPTED` rather than auto-executed, requiring explicit user `resume_task`.
3. **Node-Granular Cooperative Pause**: Cooperative pause takes effect cleanly at LangGraph superstep boundaries (between `assistant` and `tools` nodes). An already-running single tool call completes its current invocation before pausing so in-flight side effects are not left `UNKNOWN`; immediate mid-tool abort remains the semantic of `cancel_task`.

---

## 4. Regression & Verification Evidence

- `uv run pytest tests/test_sprint2b_audit_fixes.py -v`: **5 passed**
- `uv run pytest -ra`: **265 passed, 12 skipped, 0 failed** (129.26s)
