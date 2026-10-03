# Jarvis Studio - Documentation Index

Welcome to the **Jarvis Studio** documentation hub. This directory contains comprehensive technical specifications, architectural blueprints, architecture decision records (ADRs), and development guides for the Jarvis Studio personal AI operating assistant.

---

## Documentation Structure

```text
docs/
├── architecture/                     # System-level blueprints and runtime mechanics
│   ├── system-architecture.md        # 5-tier architecture, component boundaries, and data flows
│   └── execution-lifecycle.md        # StateGraph node transitions, checkpoints, and interrupt loop
├── design/                           # Subsystem-specific design specifications
│   ├── hitl-specification.md         # Human-in-the-Loop protocol, state machine, and API contracts
│   ├── tool-system-and-safety.md     # ToolRegistry, RiskLevel safety matrix, and MCP adapter
│   └── llm-adapter-layer.md          # Multi-provider LLMFactory and adapter specifications
├── development/                      # Developer workflow and configuration
│   ├── setup-and-development.md      # Environment setup, pytest guide, and CLI commands
│   └── configuration-reference.md    # Environment variables (.env) and settings.yaml catalog
├── adr/                              # Architecture Decision Records
│   ├── ADR-001-adopt-langgraph-and-mcp.md        # Transition from custom runtimes to LangGraph + MCP
│   ├── ADR-002-decouple-application-and-agent.md # Separation of API, Service, and Agent Core
│   └── ADR-003-native-interrupt-for-hitl.md      # Native LangGraph interrupt() for HITL
└── roadmap/                          # Product and engineering evolution
    └── engineering-roadmap.md        # Milestone tracking from M0 to M5
```

---

## Document Summaries

### Architecture
- **[System Architecture](file:///d:/myProject/JarvisStudio/docs/architecture/system-architecture.md)**: Explains the strict 5-layer separation (UI ↔ API Gateway ↔ Agent Service ↔ LangGraph Runtime ↔ Tools/MCP), decoupling rules, and core boundaries.
- **[Execution Lifecycle](file:///d:/myProject/JarvisStudio/docs/architecture/execution-lifecycle.md)**: Details the exact state transitions of `AgentState`, tool routing logic, checkpoint snapshotting, and the resumption loop.

### Design Specifications
- **[Human-in-the-Loop (HITL) Specification](file:///d:/myProject/JarvisStudio/docs/design/hitl-specification.md)**: Defines the approval state machine (`PENDING`, `APPROVED`, `REJECTED`, `MODIFIED`), dangerous tool interception, and the REST API contract.
- **[Tool System & Safety Matrix](file:///d:/myProject/JarvisStudio/docs/design/tool-system-and-safety.md)**: Details `ToolRegistry`, the `RiskLevel` matrix (`SAFE`, `MEDIUM`, `HIGH`), built-in file and shell tools, and dynamic MCP schema bridging.
- **[LLM Adapter Layer](file:///d:/myProject/JarvisStudio/docs/design/llm-adapter-layer.md)**: Documents `LLMFactory`, provider configurations (OpenAI, DeepSeek, Ollama, Anthropic, Gemini), and provider neutrality principles.

### Development & Operations
- **[Setup & Development Guide](file:///d:/myProject/JarvisStudio/docs/development/setup-and-development.md)**: Instructions for setting up Python 3.12 with `uv`, executing unit/integration tests, and using the `jarvis-studio` CLI.
- **[Configuration Reference](file:///d:/myProject/JarvisStudio/docs/development/configuration-reference.md)**: Exhaustive reference for all configuration options across environment variables and YAML settings.

### Architecture Decision Records (ADRs)
- **[ADR-001: Adopt LangGraph and MCP](file:///d:/myProject/JarvisStudio/docs/adr/ADR-001-adopt-langgraph-and-mcp.md)**
- **[ADR-002: Decouple Application and Agent Service](file:///d:/myProject/JarvisStudio/docs/adr/ADR-002-decouple-application-and-agent.md)**
- **[ADR-003: Native Interrupt Mechanism for HITL](file:///d:/myProject/JarvisStudio/docs/adr/ADR-003-native-interrupt-for-hitl.md)**

### Roadmap
- **[Engineering Roadmap](file:///d:/myProject/JarvisStudio/docs/roadmap/engineering-roadmap.md)**: Status of all past, current, and future milestones (M0 through M5).
