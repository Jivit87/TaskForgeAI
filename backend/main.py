from __future__ import annotations
"""
backend/main.py
FRAME-MO CLI Entry Point

Usage:
  python main.py --task "Research AI trends, save to Notion, post GitHub issue, email team"
  python main.py --task "..." --task-id abc123          # resume from checkpoint
  python main.py --task "..." --inject-failure github:rate_limit
  python main.py --list-tasks                           # show all checkpointed tasks
  python main.py --serve                                # start FastAPI for frontend dashboard
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Env loading ───────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ── Rich terminal UI ──────────────────────────────────────────────────────────
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from rich import box
from rich.progress import SpinnerColumn, TextColumn, Progress

console = Console()

# ── Internal imports ──────────────────────────────────────────────────────────
from config.mcp_configs import validate_env_keys
from core.checkpoint import CheckpointStore
from core.orchestrator import MasterOrchestrator
from tools.mcp_manager import MCPConnectionManager

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frame_mo.main")


# ── Rich UI helpers ───────────────────────────────────────────────────────────

STATUS_ICON = {
    "pending":     "⏳",
    "running":     "🔄",
    "complete":    "✅",
    "failed":      "❌",
    "skipped":     "⏭",
    "paused_hitl": "⚠️ ",
}

AGENT_COLORS = {
    "research_agent":  "cyan",
    "code_agent":      "green",
    "knowledge_agent": "magenta",
    "comms_agent":     "yellow",
}


def print_banner():
    console.print(Panel(
        Text.assemble(
            ("FRAME-MO", "bold white"),
            "\n",
            ("Fault-Resilient Agentic Multi-Orchestral Engine", "dim white"),
            "\n",
            ("Zero to One · Photon 2026 · PS1: AI Systems / Agents", "dim blue"),
        ),
        border_style="bright_blue",
        padding=(1, 4),
    ))


def print_env_check():
    missing = validate_env_keys()
    if missing:
        console.print(Panel(
            "\n".join(f"[red]✗[/red]  {k}" for k in missing),
            title="[yellow]⚠  Missing API Keys[/yellow]",
            border_style="yellow",
        ))
        console.print(
            "[dim]Tip: Copy [bold]backend/.env.example → backend/.env[/bold] "
            "and fill in your keys.\n"
            "Running in STUB mode — MCP calls will return placeholder data.[/dim]\n"
        )
    else:
        console.print("[green]✓ All API keys loaded[/green]\n")


def build_metrics_table(result: dict, state_snapshot: dict | None = None) -> Table:
    """Build the live metrics table shown during and after execution."""
    table = Table(
        title="FRAME-MO Live Metrics",
        box=box.ROUNDED,
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=True,
    )
    table.add_column("Agent",      style="bold", width=16)
    table.add_column("Status",     width=14)
    table.add_column("Retries",    justify="center", width=8)
    table.add_column("Validated",  justify="center", width=10)
    table.add_column("Output Key", width=20)

    agents = ["research_agent", "code_agent", "knowledge_agent", "comms_agent"]
    completed = result.get("completed_agents", [])
    retries   = result.get("retry_counts", {})
    results   = result.get("agent_results", {})

    for agent in agents:
        color = AGENT_COLORS.get(agent, "white")
        if agent in completed:
            status  = f"[green]{STATUS_ICON['complete']} done[/green]"
            valid   = "[green]✅[/green]"
            out_key = list(results.get(agent, {}).keys())[:2]
            out_str = ", ".join(str(k) for k in out_key) or "—"
        elif state_snapshot and state_snapshot.get("current_agent") == agent:
            status  = f"[yellow]{STATUS_ICON['running']} running[/yellow]"
            valid   = "—"
            out_str = "—"
        else:
            status  = f"[dim]{STATUS_ICON['pending']} pending[/dim]"
            valid   = "—"
            out_str = "—"

        retry_count = retries.get(agent, 0)
        retry_str   = f"[red]{retry_count}[/red]" if retry_count > 0 else "0"

        table.add_row(
            f"[{color}]{agent.replace('_agent', '')}[/{color}]",
            status,
            retry_str,
            valid,
            out_str,
        )

    return table


def print_final_result(result: dict):
    """Pretty-print the final aggregated result."""
    console.print()
    console.print(Panel(
        f"[bold white]{result.get('summary', 'Task complete.')}[/bold white]",
        title=f"[green]✅ Task Complete — {result.get('task_id')}[/green]",
        border_style="green",
        padding=(1, 2),
    ))

    errors = result.get("error_log", [])
    if errors:
        console.print(Panel(
            "\n".join(f"[red]•[/red] [{e['agent']}] {e['error']}" for e in errors),
            title="[yellow]Errors (recovered)[/yellow]",
            border_style="yellow",
        ))

    console.print(build_metrics_table(result))


def print_task_list(tasks: list[dict]):
    """Print a table of all checkpointed tasks."""
    table = Table(
        title="Checkpointed Tasks",
        box=box.ROUNDED,
        border_style="blue",
        header_style="bold cyan",
    )
    table.add_column("Task ID",          width=12)
    table.add_column("Status",           width=14)
    table.add_column("Completed Agents", width=40)
    table.add_column("Last Updated",     width=20)
    table.add_column("Goal Preview",     width=50)

    for t in tasks:
        s = t["status"]
        color = "green" if s == "complete" else ("red" if s == "failed" else "yellow")
        icon  = STATUS_ICON.get(s, "•")
        table.add_row(
            t["task_id"],
            f"[{color}]{icon} {s}[/{color}]",
            ", ".join(t["completed_agents"]) or "—",
            t["updated_at"][:19],
            t["goal_preview"],
        )
    console.print(table)


# ── Core async run function ───────────────────────────────────────────────────

async def run_task(args):
    """Run a full task with Rich live display."""
    print_banner()
    print_env_check()

    # Parse --inject-failure  e.g. github:rate_limit,notion:malformed_output
    inject = {}
    if args.inject_failure:
        for item in args.inject_failure.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                service, ftype = parts
                agent_map = {
                    "github": "code_agent",
                    "notion": "knowledge_agent",
                    "gmail":  "comms_agent",
                    "tavily": "research_agent",
                }
                if service in agent_map:
                    inject[agent_map[service]] = ftype
                    console.print(
                        f"[yellow]⚡ Failure injection:[/yellow] "
                        f"{agent_map[service]} → {ftype}"
                    )

    checkpoint = CheckpointStore(
        db_path=os.environ.get("CHECKPOINT_DB_PATH", "agent_checkpoints.db")
    )
    mcp = MCPConnectionManager()

    console.print("[dim]Connecting MCP servers...[/dim]")
    await mcp.connect_all()

    # Show MCP health
    health = mcp.get_health()
    health_str = "  ".join(
        f"[green]●[/green] {k.replace('-mcp','')}" if v in ("healthy","stub")
        else f"[red]●[/red] {k.replace('-mcp','')}"
        for k, v in health.items()
    )
    console.print(f"MCP:  {health_str}\n")

    orchestrator = MasterOrchestrator(
        checkpoint_store=checkpoint,
        mcp_manager=mcp,
        inject_failures=inject,
    )

    task_id = args.task_id or None
    console.print(
        Panel(
            f"[bold]{args.task}[/bold]",
            title=f"[cyan]▶ Task{' — resuming ' + task_id if task_id else ''}[/cyan]",
            border_style="cyan",
        )
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Running agents...", total=None)
        start = datetime.utcnow()
        result = await orchestrator.run(goal=args.task, task_id=task_id)
        elapsed = (datetime.utcnow() - start).total_seconds()

    result["elapsed_seconds"] = round(elapsed, 1)
    print_final_result(result)

    # Save JSON result
    out_path = f"result_{result['task_id']}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    console.print(f"\n[dim]Full result saved → {out_path}[/dim]")

    return result


# ── FastAPI server for frontend dashboard ─────────────────────────────────────

def start_api_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the FastAPI + WebSocket server for the frontend dashboard."""
    try:
        import uvicorn
        from api import app   # api.py created below
        console.print(f"[cyan]Starting API server on http://{host}:{port}[/cyan]")
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except ImportError:
        console.print(
            "[red]FastAPI/uvicorn not installed. "
            "Run: pip install fastapi uvicorn[/red]"
        )
        sys.exit(1)


# ── CLI argument parsing ──────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        prog="frame-mo",
        description="FRAME-MO — Fault-Resilient Agentic Multi-Orchestral Engine",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Default: run task (no subcommand needed for backwards compat)
    parser.add_argument("--task",    "-t",  type=str, help="Goal for the agent pipeline")
    parser.add_argument("--task-id", "-id", type=str, dest="task_id",
                        help="Resume an existing task by ID")
    parser.add_argument("--inject-failure", type=str, dest="inject_failure",
                        help="Inject failures for demo: github:rate_limit,notion:malformed_output")
    parser.add_argument("--list-tasks", "-l", action="store_true", dest="list_tasks",
                        help="List all checkpointed tasks")
    parser.add_argument("--serve", "-s", action="store_true",
                        help="Start FastAPI server for frontend dashboard")
    parser.add_argument("--port",  "-p", type=int, default=8000,
                        help="API server port (default: 8000)")

    return parser.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # List tasks
    if args.list_tasks:
        print_banner()
        checkpoint = CheckpointStore(
            db_path=os.environ.get("CHECKPOINT_DB_PATH", "agent_checkpoints.db")
        )
        tasks = checkpoint.list_tasks()
        if not tasks:
            console.print("[dim]No checkpointed tasks found.[/dim]")
        else:
            print_task_list(tasks)
        return

    # Start API server for frontend
    if args.serve:
        start_api_server(port=args.port)
        return

    # Run task
    if not args.task:
        console.print(
            "[red]Error:[/red] --task is required.\n"
            "Example: python main.py --task \"Research AI trends and email summary\"\n"
        )
        sys.exit(1)

    asyncio.run(run_task(args))


if __name__ == "__main__":
    main()
