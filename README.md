# FRAME-MO
> Fault-Resilient Agentic Multi-Orchestral Engine
> Zero to One | Photon 2026 — PS1: AI Systems / Agents

## Stack
- **Orchestrator:** Claude claude-sonnet-4-6 (Anthropic SDK)
- **Sub-Agents:** LLaMA 3.3-70b (Groq API)
- **MCP Servers:** Gmail · GitHub · Notion · Brave Web Search
- **Reliability:** SQLite checkpointing · Pydantic validation · Tenacity retry · HITL gate
- **Frontend:** Vanilla HTML/CSS/JS live dashboard

## Setup
```bash
cd backend
cp .env.example .env      # Fill in your API keys
pip install -r requirements.txt
python main.py --task "Your goal here"
```

## Frontend
```bash
cd frontend
npx serve . -p 3000
```

## Project Structure
```
TaskForge/
├── backend/
│   ├── core/          # Orchestrator, checkpoint, retry, HITL
│   ├── agents/        # Master + 4 sub-agents
│   ├── tools/         # Tool registry, native tools, MCP manager
│   ├── schemas/       # Pydantic models
│   ├── config/        # Prompts + MCP configs
│   └── main.py        # CLI entry point
└── frontend/
    ├── index.html
    └── src/
        ├── components/ # Dashboard, AgentCard, TaskInput, LogViewer, HITLModal
        ├── pages/      # API client
        └── styles/     # main.css
```
