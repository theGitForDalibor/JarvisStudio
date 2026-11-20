"""Tests for Research & Analysis capabilities: pluggable search, web fetch, context injection, and E2E report delivery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from jarvis_studio.agent.graph import build_agent_graph
from jarvis_studio.config.models import StorageConfig
from jarvis_studio.config.settings import Settings, get_settings
from jarvis_studio.core.runtime.agent_runtime import AgentRuntime
from jarvis_studio.core.tasks.models import DeliverableType, EventType, TaskStatus
from jarvis_studio.service.agent_service import AgentService
from jarvis_studio.service.deliverable_manager import (
    DeliverableManager,
    reset_global_deliverable_manager,
)
from jarvis_studio.tools.builtin import register_builtin_tools
from jarvis_studio.tools.builtin.search_tools import (
    CompositeSearchEngine,
    DuckDuckGoSearchProvider,
    SearchResult,
    TavilySearchProvider,
    search_web,
)
from jarvis_studio.tools.builtin.web_fetch_tools import (
    extract_readable_text_from_html,
    fetch_web_page,
    validate_public_http_url,
)
from jarvis_studio.tools.registry import ToolRegistry


class _ResearchMockModel(BaseChatModel):
    """Scripted multi-turn model capturing SystemMessage and driving search -> fetch -> publish."""

    captured_system_prompts: list[str] = []
    turn: int = 0
    task_id_holder: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "mock_research"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ResearchMockModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if messages and isinstance(messages[0], SystemMessage):
            self.captured_system_prompts.append(str(messages[0].content))
            # Extract task_id from Execution Context injected by assistant_node
            for line in str(messages[0].content).splitlines():
                if line.strip().startswith("- task_id:"):
                    tid = line.split(":", 1)[1].strip()
                    if tid not in self.task_id_holder:
                        self.task_id_holder.append(tid)

        current_turn = self.turn
        self.turn += 1

        if current_turn == 0:
            # Turn 1: Decompose and search two sub-queries
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="I will research leading AI Coding Agents across positioning, capabilities, and pricing.",
                            tool_calls=[
                                {
                                    "id": "call_search_1",
                                    "name": "search_web",
                                    "args": {"query": "Cursor vs GitHub Copilot architecture pricing 2026", "top_k": 3},
                                },
                                {
                                    "id": "call_search_2",
                                    "name": "search_web",
                                    "args": {"query": "Claude Code Devin AI coding agent technical approach", "top_k": 3},
                                },
                            ],
                        )
                    )
                ]
            )
        if current_turn == 1:
            # Turn 2: Fetch primary web pages for deeper verification
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Let me inspect the primary documentation pages from search results.",
                            tool_calls=[
                                {
                                    "id": "call_fetch_1",
                                    "name": "fetch_web_page",
                                    "args": {"url": "https://example.com/cursor-overview"},
                                },
                                {
                                    "id": "call_fetch_2",
                                    "name": "fetch_web_page",
                                    "args": {"url": "https://example.com/copilot-overview"},
                                },
                            ],
                        )
                    )
                ]
            )
        if current_turn == 2:
            # Turn 3: Publish formal Markdown report deliverable using injected task_id
            tid = self.task_id_holder[0] if self.task_id_holder else "task_unknown"
            report_md = (
                "# 主流 AI Coding Agent 深度对比分析报告\n\n"
                "## 1. 已验证事实（Verified Facts）\n"
                "- **Cursor**: 定位于 AI-native IDE（基于 VS Code 分支），支持多文件 Tab 补全与 Composer Agent 模式 "
                "([来源](https://example.com/cursor-overview))。\n"
                "- **GitHub Copilot**: 定位于跨 IDE AI 编程助手，深度集成 GitHub PR/Issue 与 Workspace Agent "
                "([来源](https://example.com/copilot-overview))。\n\n"
                "## 2. 横向对比矩阵\n"
                "| 产品 | 产品定位 | 技术路线 | 商业模式 |\n"
                "|---|---|---|---|\n"
                "| Cursor | AI-native IDE | 自研上下文索引 + 多模型路由 | Freemium 个人/团队订阅 |\n"
                "| GitHub Copilot | 生态级编程助手 | IDE 插件 + GitHub 云端 Agent | 按席位订阅制 |\n\n"
                "## 3. 分析推断与观点（Inferences & Opinions）\n"
                "- **推断**：IDE 深度集成派（Cursor）在本地上下文编辑延迟上占优，而生态平台派（Copilot）在企业合规与 CI/CD 联动上更具粘性。\n\n"
                "## 4. 信息不足与不确定性说明\n"
                "- 部分产品的企业级私有化部署具体报价未在公开页面披露，需联系销售确认。\n"
            )
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="Publishing the structured comparison report as a formal task deliverable.",
                            tool_calls=[
                                {
                                    "id": "call_publish_1",
                                    "name": "publish_research_report",
                                    "args": {
                                        "task_id": tid,
                                        "title": "主流 AI Coding Agent 对比分析报告",
                                        "markdown_content": report_md,
                                        "summary": "对比主流 AI Coding Agent 的定位、技术路线与商业模式。",
                                    },
                                }
                            ],
                        )
                    )
                ]
            )

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="已完成调研并发布对比分析报告《主流 AI Coding Agent 对比分析报告》至任务交付物。"
                    )
                )
            ]
        )


def test_duckduckgo_html_parsing_fixed_with_result_url() -> None:
    """Verify DuckDuckGoSearchProvider parses real HTML even when result__url anchors exist (regression for old bug)."""
    sample_html = """
    <div class="result results_links results_links_deep web-result">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcursor.com%2Ffeatures&amp;rut=abc">Cursor AI Features</a>
      </h2>
      <a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcursor.com%2Ffeatures">cursor.com/features</a>
      <a class="result__snippet" href="#">Cursor is an AI-powered code editor built on VS Code.</a>
    </div>
    <div class="result results_links">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=spam">Ad Link</a>
      </h2>
      <a class="result__snippet" href="#">Sponsored ad should be skipped.</a>
    </div>
    <div class="result results_links">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="https://github.com/features/copilot">GitHub Copilot &amp; Agents</a>
      </h2>
      <a class="result__url" href="https://github.com/features/copilot">github.com/features/copilot</a>
      <a class="result__snippet" href="#">Your AI pair programmer from GitHub.</a>
    </div>
    """
    provider = DuckDuckGoSearchProvider(timeout=2.0, max_retries=0)
    results = provider.parse_html(sample_html, top_k=5)
    assert len(results) == 2
    assert results[0].title == "Cursor AI Features"
    assert results[0].url == "https://cursor.com/features"
    assert "AI-powered code editor" in results[0].snippet
    assert results[1].title == "GitHub Copilot & Agents"
    assert results[1].url == "https://github.com/features/copilot"


def test_tavily_provider_and_composite_fallback() -> None:
    """Verify TavilySearchProvider parsing and fallback chain to DuckDuckGo / Local KB."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "AI Coding Agents 2026",
                "url": "https://example.org/ai-agents",
                "content": "Comprehensive overview of coding agents.",
                "published_date": "2026-05-01",
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None

    with patch("jarvis_studio.tools.builtin.search_tools.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.return_value = mock_resp

        tavily = TavilySearchProvider(api_key="tvly-fake-key", timeout=2.0, max_retries=0)
        res = tavily.search("ai coding agents", top_k=3)
        assert len(res) == 1
        assert res[0].source == "tavily"
        assert res[0].url == "https://example.org/ai-agents"
        assert res[0].published_date == "2026-05-01"

    # Fallback check: failing primary -> local KB fallback
    class _FailingPrimary:
        def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
            raise ConnectionError("network down")

    class _MockLocalKB:
        def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
            return [SearchResult(title="Local Note", url="workspace://notes.md", snippet="Local data", source="local_kb")]

    engine = CompositeSearchEngine(primary=_FailingPrimary(), fallback=_MockLocalKB())
    hits, provider = engine.search("anything", top_k=3)
    assert provider == "local_kb"
    assert len(hits) == 1
    assert hits[0].url == "workspace://notes.md"


def test_fetch_web_page_extraction_and_ssrf_defense() -> None:
    """Verify fetch_web_page extracts clean readable text and blocks private/loopback SSRF URLs."""
    # 1. SSRF checks
    for bad_url in (
        "http://127.0.0.1:8000/secret",
        "http://localhost:11434/api/tags",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/router",
        "file:///etc/passwd",
    ):
        res = fetch_web_page.invoke({"url": bad_url})
        assert "Error fetching URL" in res

    # 2. HTML extraction & truncation
    raw_html = """
    <html>
      <head><title>Agent Architecture Guide</title><style>body { color: red; }</style></head>
      <body>
        <nav>Home | Docs</nav>
        <script>console.log("ignore me");</script>
        <main>
          <h1>Core Capabilities</h1>
          <p>Supports multi-file editing &amp; autonomous tool calling.</p>
        </main>
      </body>
    </html>
    """
    title, text = extract_readable_text_from_html(raw_html)
    assert title == "Agent Architecture Guide"
    assert "Core Capabilities" in text
    assert "Supports multi-file editing & autonomous tool calling." in text
    assert "console.log" not in text
    assert "color: red" not in text

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.extensions = {}
    mock_resp.iter_bytes.return_value = [raw_html.encode("utf-8")]
    mock_resp.encoding = "utf-8"
    mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
    mock_resp.url = "https://93.184.216.34/guide"
    mock_resp.raise_for_status.return_value = None

    stream_ctx = MagicMock()
    stream_ctx.__enter__.return_value = mock_resp
    stream_ctx.__exit__.return_value = False

    with patch("jarvis_studio.tools.builtin.web_fetch_tools.httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.stream.return_value = stream_ctx
        out = fetch_web_page.invoke({"url": "https://93.184.216.34/guide", "max_chars": 500})
        assert "Title: Agent Architecture Guide" in out
        assert "Supports multi-file editing & autonomous tool calling." in out


@pytest.mark.asyncio
async def test_e2e_research_and_analysis_loop_with_deliverable_and_events(tmp_path: Path) -> None:
    """Verify General Agent completes multi-turn search -> fetch -> publish_research_report end-to-end."""
    db_file = tmp_path / "e2e_research.db"
    ws_dir = tmp_path / "workspace"
    cfg = Settings(
        storage=StorageConfig(
            checkpoint_backend="sqlite",
            sqlite_path=str(db_file),
            workspace_root=str(ws_dir),
        )
    )
    deliv_mgr = DeliverableManager(db_path=db_file, workspace_root=ws_dir)

    rt = AgentRuntime(cfg, register_as_active=True)
    await rt.start()

    try:
        coord = rt.task_coordinator
        engine = rt.execution_engine

        reg = ToolRegistry()
        register_builtin_tools(reg)

        mock_llm = _ResearchMockModel()
        graph = build_agent_graph(
            model=mock_llm,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_mgr=rt.hitl_manager,
        )
        svc = AgentService(
            graph=graph,
            registry=reg,
            checkpointer=rt.checkpointer,
            hitl_manager=rt.hitl_manager,
            task_coordinator=coord,
            session_service=rt.agent_service.session_service,
        )
        engine.set_agent_service(svc)

        def _fake_search(query: str, top_k: int = 5) -> tuple[list[SearchResult], str]:
            if "Cursor" in query:
                return (
                    [
                        SearchResult(
                            title="Cursor Overview",
                            url="https://example.com/cursor-overview",
                            snippet="AI-native IDE built on VS Code with Composer.",
                            source="mock_web",
                        )
                    ],
                    "mock_web",
                )
            return (
                [
                    SearchResult(
                        title="GitHub Copilot Overview",
                        url="https://example.com/copilot-overview",
                        snippet="Cross-IDE coding assistant integrated with GitHub.",
                        source="mock_web",
                    )
                ],
                "mock_web",
            )

        def _make_fake_fetch(url: str) -> MagicMock:
            resp = MagicMock()
            resp.status_code = 200
            resp.extensions = {}
            resp.headers = {"content-type": "text/html"}
            resp.encoding = "utf-8"
            resp.url = url
            resp.raise_for_status.return_value = None
            if "cursor" in url:
                body = b"<html><title>Cursor Docs</title><body><p>Cursor supports multi-file Composer and Pro subscription.</p></body></html>"
            else:
                body = b"<html><title>Copilot Docs</title><body><p>GitHub Copilot integrates deeply with PRs and seat-based billing.</p></body></html>"
            resp.iter_bytes.return_value = [body]
            ctx = MagicMock()
            ctx.__enter__.return_value = resp
            ctx.__exit__.return_value = False
            return ctx

        with (
            patch(
                "jarvis_studio.service.deliverable_manager.get_deliverable_manager",
                return_value=deliv_mgr,
            ),
            patch(
                "jarvis_studio.tools.builtin.search_tools._search_engine.search",
                side_effect=_fake_search,
            ),
            patch(
                "jarvis_studio.tools.builtin.web_fetch_tools.validate_public_http_url",
                side_effect=lambda u: u,
            ),
            patch("jarvis_studio.tools.builtin.web_fetch_tools.httpx.Client") as mock_http_cls,
        ):
            mock_client = mock_http_cls.return_value.__enter__.return_value
            mock_client.stream.side_effect = lambda method, u, **kw: _make_fake_fetch(u)

            task = coord.create_task(
                query="研究当前主流 AI Coding Agent 的产品定位、核心能力、技术路线和商业模式，并形成对比分析报告。"
            )
            run, fut = await engine.submit_task(task.task_id, return_future=True)
            assert fut is not None
            res = await asyncio.wait_for(fut, timeout=10.0)
            assert res.status == "completed"

        # 1. Verify Execution Context (task_id, run_id) was injected into assistant prompt
        assert len(mock_llm.captured_system_prompts) >= 1
        assert f"- task_id: {task.task_id}" in mock_llm.captured_system_prompts[0]
        assert f"- run_id: {run.run_id}" in mock_llm.captured_system_prompts[0]

        # 2. Verify Task completed cleanly
        final_task = coord.get_task(task.task_id)
        assert final_task is not None
        assert final_task.status == TaskStatus.COMPLETED

        # 3. Verify formal Deliverable persisted with citations & structured analysis
        deliverables = deliv_mgr.list_deliverables_for_task(task.task_id)
        assert len(deliverables) == 1
        deliv = deliverables[0]
        assert deliv.deliverable_type == DeliverableType.MARKDOWN_REPORT
        assert deliv.title == "主流 AI Coding Agent 对比分析报告"
        assert deliv.file_path is not None
        content_on_disk = (ws_dir / deliv.file_path).read_text(encoding="utf-8")
        assert "https://example.com/cursor-overview" in content_on_disk
        assert "https://example.com/copilot-overview" in content_on_disk
        assert "已验证事实" in content_on_disk
        assert "信息不足与不确定性" in content_on_disk

        # 4. Verify complete observability event chain
        events = coord.list_events_for_task(task.task_id)
        event_types = [e.event_type for e in events]
        assert EventType.TASK_QUEUED in event_types
        assert EventType.RUN_STARTED in event_types
        assert EventType.TOOL_STARTED in event_types
        assert EventType.TOOL_COMPLETED in event_types
        assert EventType.DELIVERABLE_CREATED in event_types
        assert EventType.TASK_COMPLETED in event_types

        tool_names = [
            json.loads(e.payload_json).get("tool_name")
            for e in events
            if e.event_type == EventType.TOOL_STARTED
        ]
        assert "search_web" in tool_names
        assert "fetch_web_page" in tool_names
        assert "publish_research_report" in tool_names
    finally:
        reset_global_deliverable_manager()
        await rt.shutdown()
