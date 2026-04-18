# TaskForge — FRAME-MO

> **Fault-Resilient Agentic Multi-Orchestral Engine**
> An autonomous AI agent that reliably executes multi-step tasks across GitHub, Notion, Gmail, and the Web — even when APIs fail.

---

## What is FRAME-MO?

Most AI agents break the moment a single API call fails. FRAME-MO was built to solve that.

It is a production-grade agentic framework that:

- **Decomposes** complex goals into typed sub-tasks using an LLM Orchestrator
- **Verifies** the generated plan against logical safety constraints before a single API call is made (Pre-Flight Gate)
- **Monitors** each sub-agent in real-time for hallucination loops and intent drift (PEI Monitor)
- **Rolls back** completed work automatically when a later step fails (Saga Recovery)
- **Streams** results token-by-token to a live React dashboard

It also handles natural conversation — if you just say "hi", you get a conversational response. No agents are invoked.

---

## Architecture

```
User Message
    │
    ▼
Intent Classifier ──── conversation ──→ Direct LLM Reply
    │
    │ task
    ▼
Planning & Decomposition  (Ollama / kimi-k2.5)
    │
    ▼
LTL Verification Gate ─── violations ──→ Force Replan (up to 2x)
    │
    │ pass
    ▼
┌─────────────────────────────────────────────┐
│               Execution Loop                │
│                                             │
│  For each step:                             │
│    Context Isolation → Sub-Agent            │
│    PEI Monitor (hallucination guard)        │
│    Output Validation (Pydantic)             │
│    State Checkpoint (SQLite)                │
└─────────────────────────────────────────────┘
    │                │
    │ success        │ failure
    ▼                ▼
Aggregate &     Saga Rollback
Respond         (compensating MCP calls)
```

---

## Sub-Agents

| Agent | MCP Server | Capabilities |
|---|---|---|
| 🔍 **Research Agent** | Tavily Search | Web search, URL fetching, content synthesis |
| 💻 **Code Agent** | GitHub MCP | List/read repos, create files, open PRs & issues |
| 📖 **Knowledge Agent** | Notion MCP | Read pages, create pages, search workspace |
| 📧 **Comms Agent** | Gmail MCP | Read threads, draft & send emails |

All agents are self-describing — adding a new agent requires only creating a subclass of `BaseAgent`.

---

## Key Reliability Features

### 🛡️ Pre-Flight LTL Verification
Before the first API call, the plan is scanned for logical constraint violations:
- No write-before-read (e.g., can't email results before research runs)
- No duplicate agent routes
- HITL required for all irreversible actions (email send, file create)

If the plan fails, the LLM is forced to rewrite it (up to 2 replanning attempts).

### 👁️ PEI Monitor (Per-Execution-Instance)
Each agent step is actively watched for:
- **Hallucination loops** — kills the agent if it calls the same tool >3 times with identical arguments
- **Tool call overflow** — hard cap of 10 tool calls per step
- **Hard timeout** — 60 seconds per agent step

### 🔄 Saga Recovery
Every completed step is checkpointed to SQLite. If a later step fails after retries, the Saga Engine walks backward through the checkpoints and fires compensating MCP calls:
- GitHub issue created → automatically closed
- Notion page created → automatically deleted
- Every rollback action is logged to `saga_log`

### 💾 Fault-Tolerant Checkpointing
All task state is persisted to SQLite after every step. You can resume any task by its ID:
```bash
python main.py --task "Research AI trends" --task-id abc12345
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Orchestrator LLM** | Ollama (local) — `kimi-k2.5:cloud` or any model |
| **Agent LLM** | Ollama (local, same instance) |
| **API** | FastAPI + WebSockets |
| **Frontend** | React (Vite) + Tailwind CSS |
| **MCP Integration** | Model Context Protocol SDK |
| **Schema Validation** | Pydantic v2 |
| **Persistence** | SQLite (checkpoints + conversation memory) |
| **Async Runtime** | Python asyncio + AsyncOpenAI |

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** and **npm**
- **Ollama** running locally — `ollama run kimi-k2.5:cloud` (or any compatible model)
- API keys for the integrations you want to use (see [Environment Setup](#environment-setup))

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/Jivit87/TaskForgeAI.git
cd TaskForgeAI
```

### 2. Backend setup

```bash
cd backend

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate   # macOS/Linux
# .venv\Scripts\activate    # Windows

# Install dependencies
pip install -r requirements.txt
```

### 3. Frontend setup

```bash
cd frontend
npm install
```

### 4. Environment setup

```bash
cd backend
cp .env.example .env
```

Open `backend/.env` and fill in your keys:

```env
# Required — Master Orchestrator
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_ORCHESTRATOR_MODEL=kimi-k2.5:cloud

# Required for Research Agent
TAVILY_API_KEY=tvly-...

# Required for Code Agent
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...

# Required for Knowledge Agent
NOTION_TOKEN=secret_...

# Optional — Communication Agent
GMAIL_OAUTH_TOKEN=...
```

> **Missing keys?** FRAME-MO starts in STUB mode for any missing integrations — you can still use all other agents while the missing ones return placeholder data.

---

## Running

### Option A: Web Dashboard (Recommended)

Start both servers:

```bash
# Terminal 1 — Backend API
cd backend
source .venv/bin/activate
python main.py --serve

# Terminal 2 — Frontend
cd frontend
npm run dev
```

Then open **http://localhost:5173** in your browser.

### Option B: CLI Mode

```bash
cd backend
source .venv/bin/activate

# Run a task
python main.py --task "Research the latest AI agent papers and create a summary in my Notion"

# Resume a failed task
python main.py --task "..." --task-id abc12345

# List all checkpointed tasks
python main.py --list-tasks
```

### Option C: Demo Failure Injection

Test the Saga rollback system without breaking production:

```bash
python main.py --task "Search Notion and create a GitHub issue" \
               --inject-failure notion:rate_limit
```

This simulates a Notion API failure mid-execution, triggering automatic rollback of any completed steps.

---

## Example Tasks

| What you type | What happens |
|---|---|
| `"what's in my github repos"` | Code Agent lists & summarizes all repositories |
| `"search my Notion for DVA notes"` | Knowledge Agent searches your Notion workspace |
| `"research latest LLM benchmarks"` | Research Agent queries Tavily, fetches pages, synthesizes results |
| `"make a README for my TaskForge-Dummy repo"` | Code Agent reads the repo and creates a `README.md` via GitHub API |
| `"hi"` / `"what can you do?"` | Direct conversational reply — no agents invoked |

---

## Project Structure

```
TaskForgeAI/
├── backend/
│   ├── main.py                  # CLI entry point
│   ├── api.py                   # FastAPI + WebSocket server
│   ├── requirements.txt
│   │
│   ├── agents/
│   │   ├── base_agent.py        # BaseAgent — async LLM loop, PEI integration
│   │   ├── research_agent.py    # Tavily web search
│   │   ├── code_agent.py        # GitHub operations
│   │   ├── knowledge_agent.py   # Notion read/write
│   │   └── comms_agent.py       # Gmail email
│   │
│   ├── core/
│   │   ├── orchestrator.py      # Master Orchestrator — planning, LTL, dispatch, aggregation
│   │   ├── ltl_verifier.py      # Pre-flight plan safety checker
│   │   ├── pei_monitor.py       # Per-Execution-Instance hallucination guard
│   │   ├── saga.py              # Saga compensating transaction engine
│   │   ├── checkpoint.py        # SQLite task state persistence
│   │   ├── memory.py            # Sliding-window conversation memory
│   │   ├── hitl.py              # Human-in-the-loop approval gate
│   │   └── retry.py             # Exponential backoff retry decorator
│   │
│   ├── schemas/
│   │   ├── agent_state.py       # AgentState — full task runtime state
│   │   ├── execution_plan.py    # ExecutionPlan + SubTask schemas
│   │   └── agent_outputs.py     # Pydantic output schemas per agent
│   │
│   ├── tools/
│   │   ├── mcp_manager.py       # MCP connection lifecycle + tool dispatch
│   │   ├── native_tools.py      # Built-in tools (summarize, confidence, etc.)
│   │   └── registry.py          # Native tool registry
│   │
│   └── config/
│       ├── mcp_configs.py       # MCP server configurations
│       └── prompts.py           # LLM system prompts per agent
│
└── frontend/
    └── src/
        ├── App.jsx              # React dashboard — task stream + metrics
        └── index.css            # UI styles
```

---

## API Reference

### `POST /tasks`
Start a new task.
```json
{
  "goal": "Research OpenAI's latest models and save a summary to Notion"
}
```

**Response:**
```json
{
  "task_id": "a1b2c3d4",
  "status": "running"
}
```

### `GET /tasks/{task_id}`
Get the current state of a task.

### `WebSocket /ws/{task_id}`
Subscribe to real-time token streaming and status updates for a running task.

**Events received:**
- `token` — streaming text chunk
- `complete` — final result with `summary`, `saga_log`, `pei_violations`
- `error` — task failure details

### `GET /health`
Returns MCP server connection health.
```json
{
  "github-mcp": "healthy",
  "notion-mcp": "healthy",
  "tavily-mcp": "healthy",
  "gmail-mcp": "stub"
}
```

---

## How Conversation Memory Works

FRAME-MO maintains a **sliding-window conversation memory** stored in SQLite:
- The last N turns are injected into the orchestrator's context on every request, enabling multi-turn conversations
- When the window overflows, older turns are automatically summarized by the LLM and stored as a compressed episodic memory
- Task episodes (goal + outcome) are separately stored for future reference

This happens in the background — it never blocks the response back to the user.

---

## Extending FRAME-MO — Adding a New Agent

1. Create `backend/agents/my_agent.py`:

```python
from agents.base_agent import BaseAgent

class MyAgent(BaseAgent):
    agent_name = "my_agent"
    agent_description = "One-liner for the LLM routing prompt"
    mcp_server = "my-mcp-server"
    tool_names = ["my_tool_1", "my_tool_2"]
    routing_parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "What to do"},
        },
        "required": ["action"],
    }
    compensating_actions = {
        "create": "Delete the resource that was created",
    }
```

2. Register it in `backend/core/orchestrator.py`:

```python
from agents.my_agent import MyAgent
AGENT_REGISTRY = {
    ...
    "my_agent": MyAgent,
}
```

3. Add its MCP config to `backend/config/mcp_configs.py`.

That's it — the orchestrator automatically discovers it, builds its routing tool, and includes it in LTL verification.

---

## Known Limitations

- **Notion search scope** — The Notion API's `/search` endpoint only indexes pages your Integration has been explicitly invited to. Deep-nested pages invisible to the integration won't appear in results.
- **Gmail MCP** — Google's hosted Gmail MCP currently requires a valid OAuth token that must be refreshed periodically. Falls back to stub mode if expired.
- **Graceful shutdown** — Pressing Ctrl+C shows an `asyncio.CancelledError` from the MCP stdio subprocess cleanup. This is a known upstream issue in the `anyio`/`mcp` libraries and does not affect task execution.
- **Model dependency** — Quality of planning, PEI detection, and output synthesis is dependent on the local Ollama model. `kimi-k2.5:cloud` is recommended for best results.

---

## License

MIT © Jivit Rana 2026
