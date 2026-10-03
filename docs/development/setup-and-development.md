# Setup & Development Guide

This guide outlines the development environment setup, execution commands, testing workflows, and engineering conventions for Jarvis Studio.

---

## 1. Prerequisites

- **Python**: Version `3.12` (Strictly pinned in `.python-version` and `pyproject.toml`)
- **Package Manager**: [`uv`](https://docs.astral.sh/uv/) (v0.10+ recommended)
- **Version Control**: Git

---

## 2. Environment Setup

### 2.1 Clone and Install
```bash
# Clone the repository
git clone <repo-url>
cd JarvisStudio

# Synchronize virtual environment with all core and dev dependencies
uv sync --extra dev
```

### 2.2 Configure Environment Variables
```bash
# Copy template configuration
cp .env.example .env

# Edit .env with your LLM API keys
# e.g., set OPENAI_API_KEY or DEEPSEEK_API_KEY
```

---

## 3. Running the Project

### 3.1 Inspect Runtime & Tools (CLI)
```bash
uv run jarvis-studio info
```
This prints the active provider, environment, HITL policy, and registered tools table.

### 3.2 Launch the API Gateway
```bash
# Start FastAPI server on port 8000
uv run jarvis-studio serve --port 8000 --reload
```
Once started, visit:
- **Interactive OpenAPI Docs**: `http://localhost:8000/docs`
- **System Health Probe**: `http://localhost:8000/api/v1/system/health`

### 3.3 Interactive Terminal Chat
```bash
uv run jarvis-studio chat
```
Launches an interactive REPL with the agent. High-risk actions will trigger visual HITL prompts.

---

## 4. Testing & Code Quality

### 4.1 Running Tests
Jarvis Studio uses `pytest` with `pytest-asyncio` for full asynchronous test execution:

```bash
# Run the complete test suite
uv run pytest

# Run with verbose output
uv run pytest -v

# Run a specific test module
uv run pytest tests/test_agent_graph.py
```

### 4.2 Linting & Formatting
```bash
# Check code style with Ruff
uv run ruff check .

# Automatically apply safe formatting fixes
uv run ruff check --fix .
uv run ruff format .
```

---

## 5. Development Guidelines

1. **Safety First**: Never create a tool with destructive side effects (file modifications, shell commands, network mutations) without annotating it with `RiskLevel.HIGH` and `requires_approval=True`.
2. **Layer Isolation**: Do not call LangGraph nodes or checkpointers directly from FastAPI route handlers. All requests must route through `AgentService` or `SessionService`.
3. **No Hardcoded Keys**: API keys and model names must always be sourced from `Settings` via `pydantic-settings`.
