# 环境搭建与开发指南 (Setup & Development Guide)

本指南面向参与 Jarvis Studio 开发的开发者，详细说明了本地开发环境初始化、测试执行、CLI 命令使用与工程编码规范。

---

## 1. 基础环境要求

- **Python 版本**：严格使用 `3.12`（在 `.python-version` 与 `pyproject.toml` 中强行锁定）
- **包管理工具**：推荐使用 [`uv`](https://docs.astral.sh/uv/)（现代高性能 Python 包管理工具）
- **代码仓库**：Git

---

## 2. 本地初始化

### 2.1 克隆与同步依赖
```bash
# 克隆代码仓库
git clone <repo-url>
cd JarvisStudio

# 使用 uv 一键创建 Python 3.12 虚拟环境并安装核心及开发依赖
uv sync --extra dev
```

### 2.2 配置环境变量
```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入你的 API Key (如 OPENAI_API_KEY 或 DEEPSEEK_API_KEY)
```

---

## 3. 运行与调试

### 3.1 查看系统与工具状态 (CLI)
```bash
uv run jarvis-studio info
```
终端将输出美观的状态面板，展示当前环境、已连接的 LLM 供应商、持久化检查点配置，以及注册工具的安全矩阵表格。

### 3.2 启动 FastAPI 应用网关
```bash
# 启动 API 服务，默认监听 8000 端口并开启热重载
uv run jarvis-studio serve --port 8000 --reload
```
服务启动后，可直接访问：
- **交互式 OpenAPI (Swagger) 文档**：`http://localhost:8000/docs`
- **系统探针健康检查**：`http://localhost:8000/api/v1/system/health`

### 3.3 交互式终端对齐 (CLI REPL)
```bash
uv run jarvis-studio chat
```
启动与 Jarvis 的纯命令行对话。如果调用了高危工具（如执行 Shell），终端将弹出红色的审批提示信息。

---

## 4. 自动化测试与代码质量

### 4.1 运行测试套件
项目使用 `pytest` 搭配 `pytest-asyncio` 进行全量异步测试：

```bash
# 运行完整测试套件
uv run pytest

# 详细模式运行
uv run pytest -v

# 单独运行状态图集成测试
uv run pytest tests/test_agent_graph.py
```

### 4.2 代码检查与格式化 (Ruff)
```bash
# 执行 Ruff 代码静态检查
uv run ruff check .

# 自动修复可安全修复的 Lint 问题
uv run ruff check --fix .
uv run ruff format .
```

---

## 5. 核心开发守则

1. **安全第一**：新增任何具有系统写入、修改或执行外部指令的工具时，**必须显式标注 `RiskLevel.HIGH` 与 `requires_approval=True`**，严禁未经审计的盲目放行。
2. **严格禁止跨层调用**：FastAPI 的路由处理函数中绝不直接操作 LangGraph 节点或检查点对象，所有执行逻辑必须通过 `AgentService` 或 `SessionService` 统一调度。
3. **严禁硬编码敏感信息**：任何 API Key、数据库连接串或服务端口均须通过 `Settings` 与环境变量解耦读取。
