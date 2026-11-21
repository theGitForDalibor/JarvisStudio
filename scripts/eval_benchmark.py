"""Lightweight Golden-Dataset Streaming Evaluation & Benchmark Suite for AgentStudio.

Implements the core streaming benchmark workflow from `agent-eval-benchmark`:
1. Concurrent / sequential SSE evaluation against `POST /api/v1/chat/stream`
2. 3D streaming latency capture: TTFT (First Think), TTFA (First Answer), TTLT (Last Token), chars/s
3. Tool recall verification against expected tools
4. Exact linear-interpolation percentiles (TP50 / TP90 / TP95 / TP99)
5. Optional test-session isolation cleanup (`--auto-cleanup`)

Usage:
    uv run python scripts/eval_benchmark.py --base-url http://127.0.0.1:8000 --auto-cleanup
    uv run python scripts/eval_benchmark.py --dataset eval_cases.json --concurrency 4 --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class EvalCase:
    """Single golden evaluation test case."""

    id: str
    query: str
    expected_tools: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    session_group: str | None = None


@dataclass
class EvalResult:
    """Measured metrics and assertions for a single case."""

    case_id: str
    session_id: str
    query: str
    ttft_ms: float | None = None
    ttfa_ms: float | None = None
    ttlt_ms: float | None = None
    chars_per_sec: float | None = None
    invoked_tools: list[str] = field(default_factory=list)
    tool_recall: float = 1.0
    keyword_hit: bool = True
    output_preview: str = ""
    error: str | None = None


DEFAULT_CASES: list[dict[str, Any]] = [
    {"id": "fast_greeting", "query": "你好", "expected_tools": [], "expected_keywords": ["AgentStudio"]},
    {"id": "fast_ping", "query": "ping", "expected_tools": [], "expected_keywords": ["pong"]},
    {
        "id": "workspace_list",
        "query": "请列出当前工作区根目录有哪些目录",
        "expected_tools": ["list_directory"],
    },
    {
        "id": "rag_search",
        "query": "请在知识库中检索 HITL 人工审批机制的设计规范",
        "expected_tools": ["search_knowledge_base"],
    },
]


def percentile(values: list[float], p: float) -> float:
    """Compute exact linear-interpolated percentile (0 <= p <= 100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 2)


def summarize_metric(values: list[float]) -> dict[str, float]:
    """Return avg, tp50, tp90, tp95, tp99 for a list of millisecond measurements."""
    if not values:
        return {"count": 0, "avg": 0.0, "tp50": 0.0, "tp90": 0.0, "tp95": 0.0, "tp99": 0.0}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "tp50": percentile(values, 50),
        "tp90": percentile(values, 90),
        "tp95": percentile(values, 95),
        "tp99": percentile(values, 99),
    }


async def run_single_case(
    client: httpx.AsyncClient,
    base_url: str,
    case: EvalCase,
    session_id: str,
) -> EvalResult:
    """Execute one streaming chat request and capture 3D latency + tool recall."""
    result = EvalResult(case_id=case.id, session_id=session_id, query=case.query)
    t0 = time.perf_counter()
    output_chunks: list[str] = []
    current_event = "message"

    try:
        async with client.stream(
            "POST",
            f"{base_url.rstrip('/')}/api/v1/chat/stream",
            json={"message": case.query, "session_id": session_id},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                result.error = f"HTTP {resp.status_code}: {body.decode('utf-8', errors='replace')[:300]}"
                return result

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line:
                    current_event = "message"
                    continue
                if line.startswith("event:"):
                    current_event = line[len("event:") :].strip()
                    continue
                if not line.startswith("data:"):
                    continue

                payload_str = line[len("data:") :].strip()
                if payload_str == "[DONE]":
                    break
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                now_ms = (time.perf_counter() - t0) * 1000.0
                if current_event == "thinking":
                    if result.ttft_ms is None:
                        result.ttft_ms = round(now_ms, 2)
                elif current_event == "token":
                    tok = str(payload.get("token") or "")
                    if tok:
                        if result.ttfa_ms is None:
                            result.ttfa_ms = round(now_ms, 2)
                        output_chunks.append(tok)
                elif current_event == "tool_start":
                    t_name = str(payload.get("tool") or "")
                    if t_name:
                        result.invoked_tools.append(t_name)
                elif current_event == "error":
                    result.error = str(payload.get("error") or "Unknown stream error")
                elif current_event == "completed":
                    result.ttlt_ms = round(now_ms, 2)
                    if not output_chunks and payload.get("output_text"):
                        output_chunks.append(str(payload["output_text"]))

        if result.ttlt_ms is None:
            result.ttlt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if result.ttft_ms is None:
            result.ttft_ms = result.ttfa_ms or result.ttlt_ms

        full_output = "".join(output_chunks)
        result.output_preview = full_output[:160]

        if result.ttfa_ms is not None and result.ttlt_ms is not None and full_output:
            gen_sec = max((result.ttlt_ms - result.ttfa_ms) / 1000.0, 0.001)
            result.chars_per_sec = round(len(full_output) / gen_sec, 2)

        if case.expected_tools:
            hits = sum(1 for t in case.expected_tools if t in result.invoked_tools)
            result.tool_recall = round(hits / len(case.expected_tools), 4)
        if case.expected_keywords:
            result.keyword_hit = all(kw in full_output for kw in case.expected_keywords)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)

    return result


async def cleanup_sessions(
    client: httpx.AsyncClient, base_url: str, session_ids: list[str]
) -> int:
    """Delete temporary eval sessions so benchmark runs do not pollute workspace history."""
    deleted = 0
    for sid in set(session_ids):
        try:
            resp = await client.delete(f"{base_url.rstrip('/')}/api/v1/sessions/{sid}")
            if resp.status_code in (200, 204):
                deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] cleanup failed for {sid}: {exc}")
    return deleted


async def run_benchmark(
    *,
    base_url: str,
    cases: list[EvalCase],
    concurrency: int,
    auto_cleanup: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run all evaluation cases with bounded concurrency and produce summary report."""
    run_tag = uuid.uuid4().hex[:8]
    sem = asyncio.Semaphore(max(1, concurrency))
    session_map = {
        c.id: f"eval_{run_tag}_{c.session_group or c.id}" for c in cases
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
        async def _worker(c: EvalCase) -> EvalResult:
            async with sem:
                return await run_single_case(client, base_url, c, session_map[c.id])

        results = await asyncio.gather(*(_worker(c) for c in cases))
        cleaned = (
            await cleanup_sessions(client, base_url, list(session_map.values()))
            if auto_cleanup
            else 0
        )

    ok_results = [r for r in results if not r.error]
    ttft_vals = [r.ttft_ms for r in ok_results if r.ttft_ms is not None]
    ttfa_vals = [r.ttfa_ms for r in ok_results if r.ttfa_ms is not None]
    ttlt_vals = [r.ttlt_ms for r in ok_results if r.ttlt_ms is not None]
    recall_vals = [r.tool_recall for r in ok_results]

    return {
        "total": len(results),
        "passed": len(ok_results),
        "failed": len(results) - len(ok_results),
        "cleaned_sessions": cleaned,
        "avg_tool_recall": round(sum(recall_vals) / len(recall_vals), 4) if recall_vals else 0.0,
        "metrics_ms": {
            "ttft": summarize_metric(ttft_vals),
            "ttfa": summarize_metric(ttfa_vals),
            "ttlt": summarize_metric(ttlt_vals),
        },
        "results": [asdict(r) for r in results],
    }


def load_cases(dataset_path: str | None) -> list[EvalCase]:
    """Load golden cases from JSON file or fall back to built-in default cases."""
    raw = (
        json.loads(Path(dataset_path).read_text(encoding="utf-8"))
        if dataset_path
        else DEFAULT_CASES
    )
    return [EvalCase(**item) for item in raw]


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentStudio Streaming Evaluation Benchmark")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="AgentStudio server URL")
    parser.add_argument("--dataset", default=None, help="Path to golden dataset JSON")
    parser.add_argument("--concurrency", type=int, default=2, help="Max concurrent eval requests")
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-request timeout seconds")
    parser.add_argument("--auto-cleanup", action="store_true", help="Delete eval_* sessions after run")
    parser.add_argument("--out", default=None, help="Optional path to write JSON report")
    args = parser.parse_args()

    cases = load_cases(args.dataset)
    report = asyncio.run(
        run_benchmark(
            base_url=args.base_url,
            cases=cases,
            concurrency=args.concurrency,
            auto_cleanup=args.auto_cleanup,
            timeout_seconds=args.timeout,
        )
    )

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
