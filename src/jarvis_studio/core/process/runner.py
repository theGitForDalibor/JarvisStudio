"""Async process orchestration engine with bounded output budgets and cancel-resilient cleanup."""

from __future__ import annotations

import asyncio
import codecs
import locale
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from jarvis_studio.core.observability.logger import logger
from jarvis_studio.core.process.models import (
    ProcessExecutionSpec,
    ProcessIsolationError,
    ProcessLaunchError,
    ProcessResult,
    ProcessStatus,
)
from jarvis_studio.core.process.platform import SpawnedProcess, spawn_isolated_process

DEFAULT_READ_CHUNK_SIZE = 8192
_TASK_SETTLE_TIMEOUT_SECONDS = 0.5


@dataclass(slots=True)
class _SharedOutputBudget:
    """Atomic single-event-loop tracker sharing one byte cap across stdout and stderr."""

    max_bytes: int
    retained_bytes: int = 0
    stdout_bytes_total: int = 0
    stderr_bytes_total: int = 0
    stdout_bytes_retained: int = 0
    stderr_bytes_retained: int = 0
    limit_exceeded: bool = False
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)

    def consume(self, stream_name: Literal["stdout", "stderr"], chunk: bytes) -> bool:
        """Record a non-empty chunk atomically (no awaits) and return True if limit just exceeded.

        Only strictly exceeding ``max_bytes`` (receiving byte ``max_bytes + 1``) marks
        ``limit_exceeded``. Output whose exact total length equals ``max_bytes`` remains valid.
        """
        chunk_len = len(chunk)
        if chunk_len == 0:
            return False

        if stream_name == "stdout":
            self.stdout_bytes_total += chunk_len
            chunks = self.stdout_chunks
        else:
            self.stderr_bytes_total += chunk_len
            chunks = self.stderr_chunks

        remaining = self.max_bytes - self.retained_bytes
        if remaining > 0:
            kept = chunk[:remaining]
            chunks.append(kept)
            kept_len = len(kept)
            self.retained_bytes += kept_len
            if stream_name == "stdout":
                self.stdout_bytes_retained += kept_len
            else:
                self.stderr_bytes_retained += kept_len

        if chunk_len > remaining:
            was_exceeded = self.limit_exceeded
            self.limit_exceeded = True
            return not was_exceeded

        return False

    def render(self) -> tuple[bytes, bytes]:
        """Materialize retained byte buffers for stdout and stderr."""
        return b"".join(self.stdout_chunks), b"".join(self.stderr_chunks)


def _decode_process_output(raw_bytes: bytes, *, truncated: bool) -> str:
    """Decode raw pipe bytes cleanly to text even when cut mid-codepoint."""
    if not raw_bytes:
        return ""

    # When truncated at a byte limit, inspect with incremental UTF-8 first so valid UTF-8
    # cut inside a multi-byte character drops only the dangling suffix instead of falling
    # back to a legacy OS codepage and corrupting the readable prefix.
    if truncated:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        try:
            return decoder.decode(raw_bytes, final=False)
        except UnicodeDecodeError:
            pass
    else:
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            pass

    preferred = locale.getpreferredencoding(False) or "utf-8"
    if preferred.lower().replace("-", "") not in {"utf8", "utf_8"}:
        try:
            if truncated:
                pref_dec = codecs.getincrementaldecoder(preferred)(errors="strict")
                return pref_dec.decode(raw_bytes, final=False)
            return raw_bytes.decode(preferred)
        except (LookupError, UnicodeDecodeError):
            pass

    if truncated:
        lossy_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        return lossy_dec.decode(raw_bytes, final=False)
    return raw_bytes.decode("utf-8", errors="replace")


async def _pump_stream(
    stream: asyncio.StreamReader,
    stream_name: Literal["stdout", "stderr"],
    budget: _SharedOutputBudget,
    overflow_event: asyncio.Event,
    chunk_size: int,
) -> None:
    """Read bounded chunks until EOF, budget overflow, or task cancellation."""
    while True:
        try:
            chunk = await stream.read(chunk_size)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Pipe closed abruptly while process tree was terminated; treat as stream end.
            break

        if not chunk:
            break

        just_exceeded = budget.consume(stream_name, chunk)
        if just_exceeded:
            overflow_event.set()
        if budget.limit_exceeded:
            break


def _force_cancel_tasks(tasks: list[asyncio.Task[Any] | None]) -> None:
    """Synchronously request cancellation on any still-pending helper tasks."""
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()


async def _cancel_and_drain_tasks(
    tasks: list[asyncio.Task[Any] | None],
    timeout: float = _TASK_SETTLE_TIMEOUT_SECONDS,
) -> None:
    """Cancel unfinished tasks and await bounded settlement so zero background tasks leak."""
    active: list[asyncio.Task[Any]] = []
    for task in tasks:
        if task is None:
            continue
        if not task.done():
            task.cancel()
        active.append(task)

    if not active:
        return

    pending_await = [t for t in active if not t.done()]
    if pending_await:
        with suppress(TimeoutError):
            await asyncio.wait(pending_await, timeout=max(timeout, 0.05))

    for task in active:
        if task.done() and not task.cancelled():
            # Retrieve any stored exception to avoid 'Task exception was never retrieved'.
            with suppress(BaseException):
                task.exception()


async def _run_cancel_resilient_cleanup(
    cleanup_fn: Callable[[], Coroutine[Any, Any, None]],
    max_wait_seconds: float,
    tasks_to_ensure_cancelled: list[asyncio.Task[Any] | None],
) -> None:
    """Guarantee bounded process cleanup finishes even under repeated Task.cancel() calls."""

    async def _bounded_cleanup() -> None:
        try:
            async with asyncio.timeout(max_wait_seconds):
                await cleanup_fn()
        except TimeoutError:
            logger.error(
                f"Process cancellation cleanup exceeded {max_wait_seconds}s cap; forcing close."
            )
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask outer CancelledError
            logger.error(f"Unexpected error during process cancellation cleanup: {exc}")
        finally:
            _force_cancel_tasks(tasks_to_ensure_cancelled)

    cleanup_task = asyncio.create_task(
        _bounded_cleanup(),
        name="process-resilient-cleanup",
    )
    try:
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Absorb repeated external Task.cancel() signals until bounded cleanup completes.
                continue
        if not cleanup_task.cancelled():
            cleanup_task.exception()
    finally:
        _force_cancel_tasks(tasks_to_ensure_cancelled)


class AsyncProcessRunner:
    """Async execution engine coordinating isolation, bounded streaming, timeout, and cancel cleanup."""

    def __init__(self, *, chunk_size: int = DEFAULT_READ_CHUNK_SIZE) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        self._chunk_size = chunk_size

    async def run(
        self,
        spec: ProcessExecutionSpec,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> ProcessResult:
        """Execute ``spec`` asynchronously and return a structured ``ProcessResult``.

        Status resolution priority (deterministic even when events race):
        1. ``asyncio.CancelledError`` -> always runs bounded cancel-resilient cleanup and re-raises.
        2. Soft ``cancel_event`` -> ``ProcessStatus.CANCELLED`` (unless overflow triggered first).
        3. Output byte budget overflow -> ``ProcessStatus.OUTPUT_LIMIT_EXCEEDED``.
        4. Wall-clock timeout -> ``ProcessStatus.TIMEOUT``.
        5. Normal exit -> ``ProcessStatus.SUCCESS`` (``exit_code == 0``) or ``ProcessStatus.FAILED``.

        Win32 Job Object failures raise ``ProcessIsolationError`` Fail-Closed without fallback.
        """
        started = time.monotonic()

        try:
            proc = await spawn_isolated_process(spec)
        except ProcessIsolationError:
            # Strict Fail-Closed boundary: never mask OS process-tree isolation failure.
            raise
        except ProcessLaunchError as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            logger.error(f"Process launch failed for {spec.argv[0]!r}: {exc}")
            return ProcessResult(
                status=ProcessStatus.FAILED,
                exit_code=None,
                duration_ms=elapsed_ms,
                error_message=str(exc),
            )

        return await self._drive_process(
            proc=proc,
            spec=spec,
            started=started,
            cancel_event=cancel_event,
        )

    async def _drive_process(
        self,
        *,
        proc: SpawnedProcess,
        spec: ProcessExecutionSpec,
        started: float,
        cancel_event: asyncio.Event | None,
    ) -> ProcessResult:
        budget = _SharedOutputBudget(spec.max_output_bytes)
        overflow_event = asyncio.Event()

        stdout_task = asyncio.create_task(
            _pump_stream(
                proc.stdout,
                "stdout",
                budget,
                overflow_event,
                self._chunk_size,
            ),
            name=f"proc-stdout-{proc.pid}",
        )
        stderr_task = asyncio.create_task(
            _pump_stream(
                proc.stderr,
                "stderr",
                budget,
                overflow_event,
                self._chunk_size,
            ),
            name=f"proc-stderr-{proc.pid}",
        )
        wait_task = asyncio.create_task(proc.wait_exit(), name=f"proc-wait-{proc.pid}")
        overflow_waiter = asyncio.create_task(
            overflow_event.wait(), name=f"proc-overflow-{proc.pid}"
        )
        cancel_waiter = (
            asyncio.create_task(cancel_event.wait(), name=f"proc-cancel-{proc.pid}")
            if cancel_event is not None
            else None
        )

        all_tasks: list[asyncio.Task[Any] | None] = [
            stdout_task,
            stderr_task,
            wait_task,
            overflow_waiter,
            cancel_waiter,
        ]

        timed_out = False
        soft_cancelled = False
        exited_naturally = False

        async def _cleanup_on_cancellation() -> None:
            term_budget = min(
                spec.cleanup_max_seconds * 0.65,
                max(spec.graceful_termination_timeout, 0.5) + spec.reap_timeout,
            )
            drain_budget = max(0.05, min(0.5, spec.cleanup_max_seconds * 0.2))
            try:
                async with asyncio.timeout(term_budget):
                    await proc.terminate_tree(
                        graceful_timeout=spec.graceful_termination_timeout
                    )
                    if not wait_task.done():
                        await asyncio.wait(
                            [wait_task],
                            timeout=min(spec.reap_timeout, term_budget),
                        )
            except TimeoutError:
                logger.warning(
                    f"Timed out waiting for cancelled process pid={proc.pid} termination."
                )
            except Exception as exc:  # noqa: BLE001 — ensure cleanup logs & continues freeing tasks/handles
                logger.warning(
                    f"Error terminating cancelled process pid={proc.pid}: {exc}"
                )
            finally:
                try:
                    await _cancel_and_drain_tasks(all_tasks, timeout=drain_budget)
                finally:
                    _force_cancel_tasks(all_tasks)
                    try:
                        await proc.close()
                    except Exception as exc:  # noqa: BLE001 — do not mask outer CancelledError
                        logger.warning(
                            f"Error closing handles after cancellation for pid={proc.pid}: {exc}"
                        )

        try:
            watch_tasks: set[asyncio.Task[Any]] = {wait_task, overflow_waiter}
            if cancel_waiter is not None:
                watch_tasks.add(cancel_waiter)

            try:
                async with asyncio.timeout(spec.timeout_seconds):
                    done, _ = await asyncio.wait(
                        watch_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_waiter is not None and cancel_waiter in done:
                        soft_cancelled = True
                    elif overflow_waiter in done or budget.limit_exceeded:
                        # Overflow detected; exit timeout context before terminating tree.
                        pass
                    elif wait_task in done:
                        exited_naturally = True
            except TimeoutError:
                if not budget.limit_exceeded:
                    timed_out = True

            # Determine active post-wait phase outside the execution timeout context so
            # post-exit drain or tree termination cannot reclassify already-triggered outcomes.
            exit_code: int | None = proc.returncode
            if exited_naturally and not budget.limit_exceeded and not soft_cancelled:
                exit_code = await self._reap_exit_code(wait_task, proc)
                hit_overflow = await self._drain_readers_bounded(
                    stdout_task,
                    stderr_task,
                    overflow_waiter=overflow_waiter,
                    drain_grace_seconds=spec.drain_grace_seconds,
                )
                if hit_overflow or budget.limit_exceeded:
                    await proc.terminate_tree(
                        graceful_timeout=spec.graceful_termination_timeout
                    )
            else:
                await self._terminate_and_drain(
                    proc=proc,
                    spec=spec,
                    wait_task=wait_task,
                    stdout_task=stdout_task,
                    stderr_task=stderr_task,
                )
                exit_code = await self._try_read_completed_exit_code(wait_task, proc)

            await _cancel_and_drain_tasks(
                all_tasks, timeout=_TASK_SETTLE_TIMEOUT_SECONDS
            )
            await proc.close()

            duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            return self._build_result(
                pid=proc.pid,
                exit_code=exit_code,
                duration_ms=duration_ms,
                budget=budget,
                timed_out=timed_out,
                soft_cancelled=soft_cancelled,
            )

        except asyncio.CancelledError:
            await _run_cancel_resilient_cleanup(
                _cleanup_on_cancellation,
                spec.cleanup_max_seconds,
                all_tasks,
            )
            raise
        except BaseException:
            await _cleanup_on_cancellation()
            raise
        finally:
            _force_cancel_tasks(all_tasks)

    async def _reap_exit_code(
        self,
        wait_task: asyncio.Task[int],
        proc: SpawnedProcess,
    ) -> int | None:
        if wait_task.done() and not wait_task.cancelled():
            exc = wait_task.exception()
            if exc is not None:
                logger.warning(f"wait_exit raised for pid={proc.pid}: {exc}")
                return proc.returncode
            return wait_task.result()
        return proc.returncode

    async def _try_read_completed_exit_code(
        self,
        wait_task: asyncio.Task[int],
        proc: SpawnedProcess,
    ) -> int | None:
        if (
            wait_task.done()
            and not wait_task.cancelled()
            and wait_task.exception() is None
        ):
            return wait_task.result()
        return proc.returncode

    async def _drain_readers_bounded(
        self,
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
        *,
        overflow_waiter: asyncio.Task[Any],
        drain_grace_seconds: float,
    ) -> bool:
        """Allow flushed pipe output up to ``drain_grace_seconds`` to finish without hanging on open child FDs.

        Returns True if ``overflow_waiter`` fired during draining.
        """
        # Yield one tick even when drain_grace_seconds == 0 so already-queued pipe callbacks run.
        await asyncio.sleep(0)
        if overflow_waiter.done():
            return True

        deadline = time.monotonic() + drain_grace_seconds
        while True:
            pending = [t for t in (stdout_task, stderr_task) if not t.done()]
            if not pending or drain_grace_seconds <= 0:
                return overflow_waiter.done()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return overflow_waiter.done()

            done, _ = await asyncio.wait(
                [*pending, overflow_waiter],
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if overflow_waiter in done or overflow_waiter.done():
                return True
            if not done:
                return False

    async def _terminate_and_drain(
        self,
        *,
        proc: SpawnedProcess,
        spec: ProcessExecutionSpec,
        wait_task: asyncio.Task[int],
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
    ) -> None:
        """Terminate process tree, wait boundedly for exit, and briefly drain flushed pipes."""
        try:
            async with asyncio.timeout(spec.cleanup_max_seconds):
                await proc.terminate_tree(
                    graceful_timeout=spec.graceful_termination_timeout
                )
                if not wait_task.done():
                    with suppress(TimeoutError):
                        await asyncio.wait(
                            [wait_task],
                            timeout=spec.reap_timeout,
                        )
                pending_readers = [
                    t for t in (stdout_task, stderr_task) if not t.done()
                ]
                if pending_readers and spec.drain_grace_seconds > 0:
                    await asyncio.wait(
                        pending_readers,
                        timeout=spec.drain_grace_seconds,
                        return_when=asyncio.ALL_COMPLETED,
                    )
        except TimeoutError:
            logger.error(
                f"Process termination cleanup timed out after {spec.cleanup_max_seconds}s for pid={proc.pid}"
            )

    def _build_result(
        self,
        *,
        pid: int,
        exit_code: int | None,
        duration_ms: float,
        budget: _SharedOutputBudget,
        timed_out: bool,
        soft_cancelled: bool,
    ) -> ProcessResult:
        stdout_raw, stderr_raw = budget.render()
        truncated = budget.limit_exceeded

        # Strict priority: CancelledError (raised) > soft CANCELLED > OUTPUT_LIMIT_EXCEEDED > TIMEOUT > FAILED > SUCCESS
        error_message: str | None = None
        if soft_cancelled:
            status = ProcessStatus.CANCELLED
            timed_out = False
            error_message = "Process execution cancelled via cancel_event."
        elif budget.limit_exceeded:
            status = ProcessStatus.OUTPUT_LIMIT_EXCEEDED
            timed_out = False
            error_message = (
                f"Combined output exceeded max_output_bytes ({budget.max_bytes}); "
                "process tree terminated."
            )
        elif timed_out:
            status = ProcessStatus.TIMEOUT
            error_message = "Process exceeded timeout_seconds and was terminated."
        elif exit_code == 0:
            status = ProcessStatus.SUCCESS
        else:
            status = ProcessStatus.FAILED
            if exit_code is not None:
                error_message = f"Process exited with non-zero code {exit_code}."
            else:
                error_message = "Process failed without a resolvable exit code."

        return ProcessResult(
            status=status,
            exit_code=exit_code,
            stdout=_decode_process_output(stdout_raw, truncated=truncated),
            stderr=_decode_process_output(stderr_raw, truncated=truncated),
            duration_ms=duration_ms,
            pid=pid,
            timed_out=timed_out,
            cancelled=soft_cancelled,
            output_truncated=truncated,
            stdout_bytes_total=budget.stdout_bytes_total,
            stderr_bytes_total=budget.stderr_bytes_total,
            stdout_bytes_retained=budget.stdout_bytes_retained,
            stderr_bytes_retained=budget.stderr_bytes_retained,
            error_message=error_message,
        )


async def run_process(
    spec: ProcessExecutionSpec,
    *,
    cancel_event: asyncio.Event | None = None,
) -> ProcessResult:
    """Convenience entrypoint running a process specification via ``AsyncProcessRunner``."""
    return await AsyncProcessRunner().run(spec, cancel_event=cancel_event)
