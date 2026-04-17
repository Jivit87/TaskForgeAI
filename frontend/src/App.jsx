import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Plus, Search, User, Paperclip, ArrowUp, PanelLeftClose, PanelLeft,
  Database, Blocks, Settings, Code, Mail, FileText, CheckCircle2,
  Loader2, AlertCircle, XCircle, RefreshCw, Wifi, WifiOff, Play,
} from 'lucide-react';

// ── Constants ──────────────────────────────────────────────────────────────────
// All API calls are relative — Vite proxy routes /tasks, /mcp, /health, /ws
// to http://localhost:8000  (see vite.config.js)

const AGENT_INFO = {
  research_agent: {
    id: 'research_agent', name: 'Research Agent', icon: Search,
    color: 'text-blue-500', bg: 'bg-blue-50', border: 'border-blue-300',
  },
  code_agent: {
    id: 'code_agent', name: 'Code Agent', icon: Code,
    color: 'text-green-500', bg: 'bg-green-50', border: 'border-green-300',
  },
  knowledge_agent: {
    id: 'knowledge_agent', name: 'Knowledge Agent', icon: FileText,
    color: 'text-purple-500', bg: 'bg-purple-50', border: 'border-purple-300',
  },
  comms_agent: {
    id: 'comms_agent', name: 'Comms Agent', icon: Mail,
    color: 'text-amber-500', bg: 'bg-amber-50', border: 'border-amber-300',
  },
};

const STATUS_BADGE = {
  complete:    { cls: 'bg-green-100 text-green-700 border-green-200',  label: 'COMPLETED' },
  failed:      { cls: 'bg-red-100 text-red-700 border-red-200',        label: 'FAILED' },
  paused_hitl: { cls: 'bg-amber-100 text-amber-700 border-amber-200',  label: 'AWAITING APPROVAL' },
  running:     { cls: 'bg-blue-100 text-blue-700 border-blue-200',     label: 'RUNNING' },
  started:     { cls: 'bg-blue-100 text-blue-700 border-blue-200',     label: 'STARTING' },
  pending:     { cls: 'bg-slate-100 text-slate-600 border-slate-200',  label: 'PENDING' },
};

const MCP_HEALTH_CLS = {
  healthy:      'bg-green-100 text-green-700',
  stub:         'bg-slate-100 text-slate-500',
  failed:       'bg-red-100 text-red-600',
  disconnected: 'bg-slate-100 text-slate-400',
  reconnecting: 'bg-amber-100 text-amber-600',
};

function emptyMetrics() {
  return {
    completed_agents: [],
    current_agent:    null,
    error_count:      0,
    hitl_pending:     [],
    agent_results:    {},
    retry_counts:     {},
  };
}

// ── App ────────────────────────────────────────────────────────────────────────

const App = () => {
  // Layout
  const [sidebarOpen, setSidebarOpen]   = useState(true);
  const [mcpModalOpen, setMcpModalOpen] = useState(false);

  // Input
  const [inputText, setInputText]       = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Task
  const [activeTask, setActiveTask]     = useState(null);  // { id, goal, status }
  const [liveMetrics, setLiveMetrics]   = useState(emptyMetrics());
  const [finalResult, setFinalResult]   = useState(null);

  // Sidebar sessions (from GET /tasks)
  const [sessions, setSessions]         = useState([]);

  // MCP health (from GET /mcp/health)
  const [mcpHealth, setMcpHealth]       = useState({});

  // HITL
  const [hitlLoading, setHitlLoading]   = useState(false);

  const wsRef           = useRef(null);
  const wsReconnects    = useRef(0);
  const messagesEndRef  = useRef(null);

  // ── Data fetching ──────────────────────────────────────────────────────────

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch('/tasks');
      if (res.ok) {
        const data = await res.json();
        setSessions(data); // already sorted DESC by updated_at from checkpoint
      }
    } catch (_) {
      // Backend not running yet — fail silently
    }
  }, []);

  const fetchMcpHealth = useCallback(async () => {
    try {
      const res = await fetch('/mcp/health');
      if (res.ok) setMcpHealth(await res.json());
    } catch (_) {}
  }, []);

  // On mount: load sessions + start MCP health polling
  useEffect(() => {
    fetchSessions();
    fetchMcpHealth();
    const interval = setInterval(fetchMcpHealth, 10_000);
    return () => clearInterval(interval);
  }, [fetchSessions, fetchMcpHealth]);

  // ── WebSocket ──────────────────────────────────────────────────────────────

  const connectWs = useCallback((taskId) => {
    if (wsRef.current) wsRef.current.close(1000);

    // Use relative WS — Vite proxy handles /ws/* → ws://localhost:8000/ws/*
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${taskId}`);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.event === 'status' || data.event === 'complete') {
        setLiveMetrics(prev => ({
          completed_agents: data.completed_agents ?? prev.completed_agents,
          current_agent:    data.current_agent    ?? null,
          error_count:      data.error_count      ?? prev.error_count,
          hitl_pending:     data.hitl_pending      ?? prev.hitl_pending,
          agent_results:    data.agent_results    ?? prev.agent_results,
          retry_counts:     data.retry_counts     ?? prev.retry_counts,
        }));

        if (data.status) {
          setActiveTask(t => t ? { ...t, status: data.status } : null);
        }

        if (data.event === 'complete' && data.result) {
          setFinalResult(data.result);
          fetchSessions(); // refresh sidebar list
        }
      }

      if (data.event === 'error') {
        setActiveTask(t => t ? { ...t, status: 'failed' } : null);
        fetchSessions();
      }
    };

    ws.onclose = (e) => {
      if (e.code !== 1000 && wsReconnects.current < 5) {
        // Exponential back-off reconnect (2s, 4s, 8s …)
        wsReconnects.current += 1;
        setTimeout(() => connectWs(taskId), 2000 * wsReconnects.current);
      }
    };

    ws.onerror = () => {};
    wsRef.current    = ws;
    wsReconnects.current = 0;
  }, [fetchSessions]);

  // Connect WS whenever a task is set (and has a real id)
  useEffect(() => {
    if (!activeTask?.id || activeTask.id === 'pending') return;
    connectWs(activeTask.id);
    return () => { if (wsRef.current) wsRef.current.close(1000); };
  }, [activeTask?.id, connectWs]);

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [liveMetrics, finalResult]);

  // ── Actions ────────────────────────────────────────────────────────────────

  const handleSubmit = async () => {
    if (!inputText.trim() || isSubmitting) return;
    setIsSubmitting(true);
    const goal = inputText.trim();
    setInputText('');
    setFinalResult(null);

    try {
      const res = await fetch('/tasks', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ goal }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      setActiveTask({ id: data.task_id, goal: data.goal, status: data.status });
      setLiveMetrics(emptyMetrics());
    } catch (err) {
      alert(
        `Could not connect to the FRAME-MO backend.\n\n` +
        `Make sure it is running:\n  cd backend && python main.py --serve\n\n` +
        `Error: ${err.message}`
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const clearTask = () => {
    if (wsRef.current) wsRef.current.close(1000);
    setActiveTask(null);
    setLiveMetrics(emptyMetrics());
    setFinalResult(null);
    setInputText('');
  };

  // Open a past session from the sidebar
  const handleSessionClick = async (session) => {
    if (wsRef.current) wsRef.current.close(1000);
    setFinalResult(null);
    setLiveMetrics(emptyMetrics());

    try {
      // Fetch live status from API to get full details
      const res = await fetch(`/tasks/${session.task_id}`);
      if (res.ok) {
        const status = await res.json();
        setActiveTask({ id: status.task_id, goal: status.goal, status: status.status });
        setLiveMetrics(prev => ({
          ...prev,
          completed_agents: status.completed_agents || [],
          current_agent:    status.current_agent    || null,
          error_count:      status.error_count      || 0,
          hitl_pending:     status.hitl_pending      || [],
          retry_counts:     status.retry_counts     || {},
        }));
        // Only reconnect WS if task is still active
        if (!['complete', 'failed'].includes(status.status)) {
          connectWs(status.task_id);
        }
        return;
      }
    } catch (_) {}

    // Fallback: use sidebar data
    setActiveTask({
      id:     session.task_id,
      goal:   session.goal_preview,
      status: session.status,
    });
  };

  // HITL approve / reject — calls POST /tasks/{id}/hitl
  const handleHITL = async (approved) => {
    if (!activeTask?.id || hitlLoading) return;
    setHitlLoading(true);
    try {
      const res = await fetch(`/tasks/${activeTask.id}/hitl`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ approved }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // The WS stream will reflect the updated state automatically
    } catch (e) {
      alert(`Failed to submit decision: ${e.message}`);
    } finally {
      setHitlLoading(false);
    }
  };

  // ── Helpers ────────────────────────────────────────────────────────────────

  const isTaskActive = activeTask && !['complete', 'failed'].includes(activeTask.status);

  const statusBadge = (status) => {
    const cfg = STATUS_BADGE[status] || STATUS_BADGE.pending;
    const spinning = ['running', 'started'].includes(status);
    return (
      <span className={`px-2 py-1 rounded-md text-[11px] font-semibold border flex items-center gap-1 ${cfg.cls}`}>
        {spinning
          ? <Loader2 size={11} className="animate-spin" />
          : status === 'complete'
            ? <CheckCircle2 size={11} />
            : status === 'failed'
              ? <XCircle size={11} />
              : status === 'paused_hitl'
                ? <AlertCircle size={11} />
                : null}
        {cfg.label}
      </span>
    );
  };

  // ── MCP health chip (header) ───────────────────────────────────────────────

  const renderMcpChip = () => {
    if (!Object.keys(mcpHealth).length) return null;
    const allGood = Object.values(mcpHealth).every(v => v === 'healthy' || v === 'stub');
    return (
      <button
        onClick={() => setMcpModalOpen(true)}
        className={`px-2 py-1 text-xs rounded-md border flex items-center gap-1.5 font-medium transition-colors hover:brightness-95 ${allGood ? 'bg-green-50 text-green-700 border-green-200' : 'bg-red-50 text-red-700 border-red-200'}`}
      >
        {allGood ? <Wifi size={11} /> : <WifiOff size={11} />}
        MCP {allGood ? 'Live' : 'Partial'}
      </button>
    );
  };

  // ── System status indicator (header) ──────────────────────────────────────

  const renderSystemStatus = () => {
    let cls, dot, label;
    if (liveMetrics.current_agent) {
      cls   = 'bg-amber-50 text-amber-700 border-amber-200';
      dot   = 'bg-amber-500 animate-bounce';
      label = 'EXECUTING';
    } else if (activeTask?.status === 'complete') {
      cls   = 'bg-slate-50 text-slate-500 border-slate-200';
      dot   = 'bg-slate-400';
      label = 'COMPLETE';
    } else if (activeTask?.status === 'failed') {
      cls   = 'bg-red-50 text-red-600 border-red-200';
      dot   = 'bg-red-500';
      label = 'FAILED';
    } else if (activeTask?.status === 'paused_hitl') {
      cls   = 'bg-amber-50 text-amber-700 border-amber-200';
      dot   = 'bg-amber-500 animate-pulse';
      label = 'AWAITING HITL';
    } else {
      cls   = 'bg-green-50 text-green-700 border-green-200';
      dot   = 'bg-green-500 animate-pulse';
      label = 'SYSTEM ONLINE';
    }
    return (
      <div className={`px-2 py-1 text-xs font-medium rounded-md flex items-center gap-1.5 border ${cls}`}>
        <div className={`w-1.5 h-1.5 rounded-full ${dot}`} />
        {label}
      </div>
    );
  };

  // ── Pipeline view ──────────────────────────────────────────────────────────

  const renderPipelineCards = () => (
    <div className="w-full max-w-3xl mx-auto flex flex-col gap-4 mt-6 px-4 pb-6">

      {/* Goal banner */}
      <div className="bg-slate-50 border border-slate-200 rounded-xl p-5 shadow-sm">
        <h3 className="text-[10px] uppercase tracking-widest font-semibold text-slate-400 mb-2">Active Goal</h3>
        <p className="text-slate-800 text-[15px] font-medium leading-relaxed">{activeTask.goal}</p>
        <div className="flex items-center gap-2 mt-3 flex-wrap">
          <span className="px-2 py-1 bg-white border border-slate-200 rounded-md text-[10px] font-mono text-slate-400">
            {activeTask.id}
          </span>
          {statusBadge(activeTask.status)}
          {liveMetrics.error_count > 0 && (
            <span className="px-2 py-1 bg-amber-50 text-amber-700 border border-amber-200 rounded-md text-[11px] font-semibold flex items-center gap-1">
              <AlertCircle size={10} />
              {liveMetrics.error_count} error{liveMetrics.error_count !== 1 ? 's' : ''} (recovered)
            </span>
          )}
        </div>
      </div>

      {/* Agent cards */}
      <div>
        <h3 className="text-[10px] uppercase tracking-widest font-semibold text-slate-400 mb-3 ml-1">
          Live Execution Pipeline
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {Object.values(AGENT_INFO).map((agent) => {
            const isComplete = liveMetrics.completed_agents.includes(agent.id);
            const isRunning  = liveMetrics.current_agent === agent.id;
            const retries    = liveMetrics.retry_counts?.[agent.id] || 0;
            const Icon = agent.icon;

            let cardCls  = 'border-slate-200 bg-white opacity-55';
            let iconCls  = 'bg-slate-100 text-slate-400';
            let stateTxt = 'Pending';
            let stateCls = 'text-slate-400';

            if (isRunning) {
              cardCls  = `border-2 ${agent.border} bg-white shadow-md`;
              iconCls  = `${agent.bg} ${agent.color} animate-pulse`;
              stateTxt = 'Running...';
              stateCls = agent.color;
            } else if (isComplete) {
              cardCls  = 'border-green-200 bg-green-50/40';
              iconCls  = 'bg-green-100 text-green-600';
              stateTxt = 'Done';
              stateCls = 'text-green-600';
            }

            return (
              <div key={agent.id} className={`rounded-xl border p-4 transition-all duration-300 ${cardCls}`}>
                <div className="flex items-start gap-3 mb-3">
                  <div className={`w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 transition-all ${iconCls}`}>
                    <Icon size={20} strokeWidth={2} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <h4 className="font-semibold text-slate-800 text-[15px]">{agent.name}</h4>
                    <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                      {isRunning  && <Loader2 size={11} className={`animate-spin ${agent.color}`} />}
                      {isComplete && <CheckCircle2 size={11} className="text-green-600" />}
                      <span className={`text-[11px] font-semibold uppercase tracking-widest ${stateCls}`}>{stateTxt}</span>
                      {retries > 0 && (
                        <span className="ml-auto text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-semibold flex items-center gap-0.5">
                          <RefreshCw size={9} /> {retries} retry
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="h-9 text-[13px] border-t border-slate-100 pt-3">
                  {isComplete
                    ? <span className="text-slate-500 opacity-75">Task executed successfully.</span>
                    : isRunning
                      ? <span className="text-slate-500 animate-pulse">Processing context...</span>
                      : <span className="text-slate-300">Awaiting orchestrator routing</span>
                  }
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* HITL alert — wired to POST /tasks/{id}/hitl */}
      {liveMetrics.hitl_pending?.length > 0 && (
        <div className="p-4 bg-amber-50 border border-amber-200 rounded-xl shadow-sm text-amber-900 flex items-start gap-3 animate-in fade-in slide-in-from-bottom-2">
          <AlertCircle className="mt-0.5 shrink-0 animate-bounce" size={18} />
          <div className="flex flex-col gap-2.5 w-full">
            <h4 className="font-semibold text-[15px]">Human Approval Required</h4>
            <p className="text-[13px] leading-relaxed opacity-90">
              FRAME-MO paused before an irreversible action. Review and approve or reject.
            </p>
            {liveMetrics.hitl_pending.map((req, i) => (
              <div key={i} className="text-xs font-mono bg-white border border-amber-100 p-3 rounded-lg space-y-1">
                <div className="text-amber-700 font-semibold">
                  Agent: {req.agent || req.action?.tool || 'Unknown'}
                </div>
                {req.reason && (
                  <div className="text-slate-500">Reason: {req.reason}</div>
                )}
                <div className="text-slate-600 break-all">
                  Input: {JSON.stringify(req.action?.input || {})}
                </div>
              </div>
            ))}
            <div className="flex gap-2 mt-1">
              <button
                id="hitl-approve-btn"
                onClick={() => handleHITL(true)}
                disabled={hitlLoading}
                className="px-4 py-1.5 bg-amber-600 text-white rounded-md text-[13px] font-semibold shadow-sm hover:bg-amber-700 transition disabled:opacity-50 flex items-center gap-1.5"
              >
                {hitlLoading
                  ? <Loader2 size={12} className="animate-spin" />
                  : <CheckCircle2 size={12} />}
                Approve
              </button>
              <button
                id="hitl-reject-btn"
                onClick={() => handleHITL(false)}
                disabled={hitlLoading}
                className="px-4 py-1.5 bg-white border border-amber-300 text-amber-700 rounded-md text-[13px] font-semibold shadow-sm hover:bg-amber-50 transition disabled:opacity-50 flex items-center gap-1.5"
              >
                <XCircle size={12} />
                Reject
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Final result panel */}
      {finalResult && activeTask.status === 'complete' && (
        <div className="p-5 bg-green-50 border border-green-200 rounded-xl animate-in fade-in slide-in-from-bottom-2">
          <h4 className="font-semibold text-green-800 text-[15px] mb-2 flex items-center gap-2">
            <CheckCircle2 size={16} className="text-green-600" />
            Task Complete
          </h4>
          {finalResult.summary && (
            <p className="text-[13px] text-green-900 leading-relaxed">{finalResult.summary}</p>
          )}
          {finalResult.highlights?.length > 0 && (
            <ul className="mt-3 space-y-1">
              {finalResult.highlights.map((h, i) => (
                <li key={i} className="text-[12px] text-green-800 flex items-start gap-1.5">
                  <span className="text-green-500 mt-[2px]">•</span>{h}
                </li>
              ))}
            </ul>
          )}
          <button
            onClick={clearTask}
            className="mt-4 text-[12px] text-green-700 underline underline-offset-2 hover:text-green-900 transition"
          >
            Start a new task
          </button>
        </div>
      )}

      <div ref={messagesEndRef} />
    </div>
  );

  // ── Main render ────────────────────────────────────────────────────────────

  return (
    <div className="flex h-screen bg-claude-bg font-sans text-claude-text">

      {/* ── Sidebar ────────────────────────────────────────────────────────── */}
      {sidebarOpen && (
        <div className="w-[260px] bg-claude-sidebar border-r border-claude-border flex flex-col flex-shrink-0">

          {/* Branding + collapse */}
          <div className="p-3 flex items-center justify-between">
            <button className="flex-1 flex justify-start items-center gap-2 px-3 py-2 hover:bg-black/5 rounded-md text-sm font-medium transition-colors">
              <div className="w-6 h-6 bg-[#d97757]/10 text-[#d97757] flex items-center justify-center rounded">
                <Blocks size={14} />
              </div>
              FRAME-MO
            </button>
            <button
              onClick={() => setSidebarOpen(false)}
              className="p-2 hover:bg-black/5 rounded-md text-claude-subtext transition-colors"
            >
              <PanelLeftClose size={18} />
            </button>
          </div>

          {/* New session */}
          <div className="px-3 pb-3">
            <button
              onClick={clearTask}
              className="w-full flex items-center gap-2 px-3 py-2 bg-white hover:bg-white text-sm rounded-md border border-claude-border shadow-sm transition-all focus:outline-none focus:ring-1 focus:ring-black/10"
            >
              <Plus size={16} className="text-claude-subtext" />
              New session
            </button>
          </div>

          {/* MCP health badges */}
          {Object.keys(mcpHealth).length > 0 && (
            <div className="px-4 pb-3">
              <div className="text-[10px] font-semibold text-claude-subtext uppercase tracking-widest mb-1.5">MCP Status</div>
              <div className="flex flex-wrap gap-1">
                {Object.entries(mcpHealth).map(([srv, st]) => (
                  <span
                    key={srv}
                    title={`${srv}: ${st}`}
                    className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${MCP_HEALTH_CLS[st] || MCP_HEALTH_CLS.disconnected}`}
                  >
                    {srv.replace('-mcp', '')} {st === 'healthy' ? '●' : st === 'stub' ? '○' : '✗'}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Session list from GET /tasks */}
          <div className="flex-1 overflow-y-auto mt-1 px-3">
            <div className="text-[10px] font-semibold text-claude-subtext px-2 mb-2 uppercase tracking-widest">
              {sessions.length ? 'Recent Sessions' : 'No sessions yet'}
            </div>
            {sessions.slice(0, 25).map((s) => {
              const isActive = activeTask?.id === s.task_id;
              return (
                <button
                  key={s.task_id}
                  onClick={() => handleSessionClick(s)}
                  className={`w-full text-left px-3 py-2 text-sm rounded-md transition-colors group mb-0.5 ${isActive ? 'bg-black/5 font-medium' : 'hover:bg-black/5'}`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                      s.status === 'complete' ? 'bg-green-500' :
                      s.status === 'failed'   ? 'bg-red-500' :
                      s.status === 'paused_hitl' ? 'bg-amber-500' :
                      'bg-blue-400 animate-pulse'
                    }`} />
                    <span className="truncate text-[13px] text-claude-text">{s.goal_preview}</span>
                  </div>
                  <div className="text-[10px] text-claude-subtext pl-3 mt-0.5">
                    {s.task_id} · {s.updated_at?.slice(0, 10)}
                  </div>
                </button>
              );
            })}
          </div>

          {/* Bottom nav */}
          <div className="p-3 border-t border-claude-border">
            <button className="w-full flex items-center gap-2 px-3 py-2 hover:bg-black/5 rounded-md text-sm text-claude-text">
              <Settings size={16} className="text-claude-subtext" /> Settings
            </button>
            <button className="w-full flex items-center gap-2 px-3 py-2 hover:bg-black/5 rounded-md text-sm text-claude-text mt-1">
              <User size={16} className="text-claude-subtext" /> User Profile
            </button>
          </div>
        </div>
      )}

      {/* ── Main content ───────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col h-screen min-w-0 bg-white">

        {/* Header */}
        <div className="h-14 flex items-center justify-between px-4 shrink-0 border-b border-black/5">
          <div className="flex items-center gap-2">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="p-2 hover:bg-black/5 rounded-md text-claude-subtext transition-colors"
              >
                <PanelLeft size={18} />
              </button>
            )}
            <span className="font-semibold text-[15px]">FRAME-MO: Multi-Orchestral Agent</span>
          </div>
          <div className="flex items-center gap-2">
            {renderMcpChip()}
            {renderSystemStatus()}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto flex flex-col w-full">
          {activeTask ? renderPipelineCards() : (
            <div className="flex-1 flex flex-col items-center justify-center min-h-[50vh]">
              <div className="text-center max-w-lg px-4 flex flex-col items-center animate-in fade-in zoom-in-95 duration-500">
                <div className="w-14 h-14 bg-[#d97757] text-white rounded-2xl flex items-center justify-center mb-6 shadow-sm ring-4 ring-[#d97757]/10">
                  <Blocks size={28} />
                </div>
                <h1 className="text-[32px] font-medium text-claude-text mb-4 tracking-tight">Good morning.</h1>
                <p className="text-claude-subtext text-center text-[15px] leading-relaxed">
                  I am FRAME-MO, a multi-orchestral agent framework. What task would you like to build, research, or automate today?
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Input area */}
        <div className="w-full max-w-3xl mx-auto px-4 pb-6 pt-2 shrink-0">
          <div className={`relative flex flex-col rounded-2xl border bg-[#f1f1f1]/50 shadow-sm overflow-hidden transition-all duration-200 ${
            isTaskActive
              ? 'border-black/5'
              : 'border-claude-border focus-within:ring-1 focus-within:ring-black/10 focus-within:border-black/20 focus-within:bg-white'
          }`}>
            <textarea
              id="task-input"
              rows={isTaskActive ? 1 : 3}
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
              }}
              placeholder={
                isTaskActive ? 'Task is running — cannot start a new task yet.' : 'Message FRAME-MO...'
              }
              disabled={isSubmitting || isTaskActive}
              className="w-full resize-none bg-transparent px-4 py-4 pr-12 text-[15px] outline-none text-claude-text placeholder-claude-subtext disabled:opacity-50"
            />
            <div className="flex items-center justify-between px-3 pb-3">
              <div className="flex items-center gap-1.5 opacity-80">
                <button className="p-1.5 text-claude-subtext hover:text-claude-text rounded-md hover:bg-black/5 transition-colors">
                  <Paperclip size={18} />
                </button>
                <button
                  id="mcp-modal-btn"
                  onClick={() => setMcpModalOpen(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1 text-claude-subtext hover:text-claude-text rounded-md hover:bg-black/5 transition-colors border border-transparent hover:border-black/10"
                >
                  <Database size={15} />
                  <span className="text-sm font-medium">MCP</span>
                </button>
              </div>
              <button
                id="submit-btn"
                onClick={handleSubmit}
                disabled={!inputText.trim() || isSubmitting || isTaskActive}
                className={`p-2 rounded-xl flex items-center justify-center transition-all ${
                  inputText.trim() && !isSubmitting && !isTaskActive
                    ? 'bg-[#d97757] text-white hover:bg-[#c96a4b] shadow-sm hover:-translate-y-[1px]'
                    : 'bg-black/5 text-black/20 cursor-not-allowed'
                }`}
              >
                {isSubmitting
                  ? <Loader2 size={18} strokeWidth={2.5} className="animate-spin" />
                  : <ArrowUp size={18} strokeWidth={2.5} />
                }
              </button>
            </div>
          </div>
          <div className="text-center mt-3 text-[13px] text-claude-subtext">
            FRAME-MO executes via local Ollama inference. Outputs dynamically render via WebSocket.
          </div>
        </div>
      </div>

      {/* ── MCP Modal — live health from GET /mcp/health ───────────────────── */}
      {mcpModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-[420px] overflow-hidden flex flex-col animate-in fade-in zoom-in-95 duration-200">
            <div className="px-5 py-4 border-b border-claude-border flex justify-between items-center">
              <div className="flex items-center gap-2 font-medium text-[15px]">
                <Database size={18} className="text-[#d97757]" />
                MCP Connection Status
              </div>
              <button
                onClick={() => setMcpModalOpen(false)}
                className="text-claude-subtext hover:text-claude-text p-1 hover:bg-black/5 rounded-md"
              >
                &times;
              </button>
            </div>
            <div className="p-5 flex flex-col gap-4">
              <p className="text-sm text-claude-subtext">
                Live MCP server health. <strong>Healthy</strong> = connected with real API key.&nbsp;
                <strong>Stub</strong> = demo mode (no keys needed).
              </p>
              {Object.keys(mcpHealth).length > 0 ? (
                <div className="flex flex-col gap-2">
                  {Object.entries(mcpHealth).map(([srv, st]) => {
                    const Icon = srv.includes('github') ? Code
                               : srv.includes('tavily') ? Search
                               : srv.includes('notion') ? FileText
                               : Mail;
                    return (
                      <div key={srv} className="flex items-center justify-between p-3 border border-claude-border rounded-xl">
                        <div className="flex items-center gap-3">
                          <div className={`w-8 h-8 rounded-full flex items-center justify-center ${
                            st === 'healthy' ? 'bg-green-100 text-green-600' :
                            st === 'stub'    ? 'bg-slate-100 text-slate-500' :
                                              'bg-red-100 text-red-500'
                          }`}>
                            <Icon size={15} />
                          </div>
                          <div>
                            <div className="font-medium text-[14px]">
                              {srv.replace('-mcp', '').replace(/\b\w/g, c => c.toUpperCase())} MCP
                            </div>
                            <div className="text-[11px] text-claude-subtext capitalize">{st}</div>
                          </div>
                        </div>
                        <span className={`text-[11px] px-2 py-1 rounded-full font-semibold ${MCP_HEALTH_CLS[st] || MCP_HEALTH_CLS.disconnected}`}>
                          {st}
                        </span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-[13px] text-slate-400 text-center py-4">
                  Backend not connected.
                  <br />
                  <code className="bg-slate-100 px-1.5 py-0.5 rounded text-[12px] mt-1 inline-block">
                    cd backend && python main.py --serve
                  </code>
                </p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default App;
