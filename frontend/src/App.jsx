import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Plus, Search, User, Paperclip, ArrowUp, PanelLeftClose, PanelLeft,
  Database, Blocks, Settings, Code, Mail, FileText, CheckCircle2,
  Loader2, AlertCircle, XCircle, RefreshCw, Workflow,
  ChevronDown, ChevronRight, ShieldAlert, Undo2
} from 'lucide-react';

// ── Constants ──────────────────────────────────────────────────────────────────

const AGENT_INFO = {
  research_agent:  { name: 'Research Agent',  icon: Search },
  code_agent:      { name: 'Code Agent',      icon: Code },
  knowledge_agent: { name: 'Knowledge Agent', icon: FileText },
  comms_agent:     { name: 'Comms Agent',     icon: Mail },
};

function emptyMetrics() {
  return {
    completed_agents: [],
    current_agent:    null,
    error_count:      0,
    hitl_pending:     [],
    agent_results:    {},
    retry_counts:     {},
    saga_log:         [],
    pei_violations:   [],
    direct_reply:     '',
    intent_type:      'task',
  };
}

// ── Components ─────────────────────────────────────────────────────────────────

const AgentLogLine = ({ agentId, status, retryCount }) => {
  const info = AGENT_INFO[agentId] || { name: agentId, icon: Blocks };
  const Icon = info.icon;
  return (
    <div className="flex items-center gap-2 text-sm py-1.5 px-3 hover:bg-slate-50 transition-colors">
      <Icon size={14} className={status === 'running' ? 'text-[#d97757] animate-pulse' : 'text-slate-400'} />
      <span className={`font-medium ${status === 'running' ? 'text-slate-800' : 'text-slate-600'}`}>
        {info.name}
      </span>
      <span className="flex-1 text-slate-400 text-xs truncate">
        {status === 'running' ? 'Processing...' : status === 'complete' ? 'Done' : 'Queued'}
      </span>
      {status === 'running' && <Loader2 size={12} className="animate-spin text-[#d97757]" />}
      {status === 'complete' && <CheckCircle2 size={14} className="text-green-500" />}
      {retryCount > 0 && (
        <span className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-semibold flex items-center gap-0.5">
          <RefreshCw size={9} /> {retryCount}
        </span>
      )}
    </div>
  );
};

// ── App ────────────────────────────────────────────────────────────────────────

const App = () => {
  const [sidebarOpen, setSidebarOpen]   = useState(true);
  const [mcpModalOpen, setMcpModalOpen] = useState(false);
  const [inputText, setInputText]       = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [activeTask, setActiveTask]     = useState(null);
  const [liveMetrics, setLiveMetrics]   = useState(emptyMetrics());
  const [finalResult, setFinalResult]   = useState(null);
  const [sessions, setSessions]         = useState([]);
  const [mcpHealth, setMcpHealth]       = useState({});
  const [hitlLoading, setHitlLoading]   = useState(false);
  const [agentLogsExpanded, setAgentLogsExpanded] = useState(true);

  const wsRef           = useRef(null);
  const wsReconnects    = useRef(0);
  const messagesEndRef  = useRef(null);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch('/tasks');
      if (res.ok) setSessions(await res.json());
    } catch (_) {}
  }, []);

  const fetchMcpHealth = useCallback(async () => {
    try {
      const res = await fetch('/mcp/health');
      if (res.ok) setMcpHealth(await res.json());
    } catch (_) {}
  }, []);

  useEffect(() => {
    fetchSessions();
    fetchMcpHealth();
    const interval = setInterval(fetchMcpHealth, 10_000);
    return () => clearInterval(interval);
  }, [fetchSessions, fetchMcpHealth]);

  const connectWs = useCallback((taskId) => {
    if (wsRef.current) wsRef.current.close(1000);
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${taskId}`);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.event === 'status') {
        setLiveMetrics(prev => ({
          completed_agents: data.completed_agents ?? prev.completed_agents,
          current_agent:    data.current_agent    ?? null,
          error_count:      data.error_count      ?? prev.error_count,
          hitl_pending:     data.hitl_pending     ?? prev.hitl_pending,
          agent_results:    data.agent_results    ?? prev.agent_results,
          retry_counts:     data.retry_counts     ?? prev.retry_counts,
          saga_log:         data.saga_log         ?? prev.saga_log,
          pei_violations:   data.pei_violations   ?? prev.pei_violations,
          direct_reply:     data.direct_reply     ?? prev.direct_reply,
          intent_type:      data.intent_type      ?? prev.intent_type,
        }));
        if (data.status) setActiveTask(t => t ? { ...t, status: data.status } : null);
      }

      if (data.event === 'complete' && data.result) {
        setFinalResult(data.result);
        setActiveTask(t => t ? { ...t, status: 'complete' } : null);
        setAgentLogsExpanded(false);
        fetchSessions();
      }

      if (data.event === 'error') {
        setActiveTask(t => t ? { ...t, status: 'failed' } : null);
        fetchSessions();
      }

      // Stop reconnecting on terminal states
      // Stop reconnecting on terminal states
      if (data.event === 'terminal') {
        wsReconnects.current = 99; // prevent reconnect
      }
    };

    ws.onclose = (e) => {
      if (e.code !== 1000 && wsReconnects.current < 5) {
        wsReconnects.current += 1;
        setTimeout(() => connectWs(taskId), 2000 * wsReconnects.current);
      }
    };

    wsRef.current = ws;
    wsReconnects.current = 0;
  }, [fetchSessions]);

  useEffect(() => {
    if (!activeTask?.id || activeTask.id === 'pending') return;
    if (['complete', 'failed'].includes(activeTask.status)) return;
    connectWs(activeTask.id);
    return () => { if (wsRef.current) wsRef.current.close(1000); };
  }, [activeTask?.id, connectWs]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [liveMetrics, finalResult]);
  const handleSubmit = async () => {
    if (!inputText.trim() || isSubmitting) return;
    setIsSubmitting(true);
    const goal = inputText.trim();
    setInputText('');
    setFinalResult(null);
    setAgentLogsExpanded(true);

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
      alert(`Could not connect to the FRAME-MO backend.\nError: ${err.message}`);
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

  const handleSessionClick = async (session) => {
    if (wsRef.current) wsRef.current.close(1000);
    setFinalResult(null);
    setLiveMetrics(emptyMetrics());
    setAgentLogsExpanded(session.status !== 'complete');

    try {
      const res = await fetch(`/tasks/${session.task_id}`);
      if (res.ok) {
        const status = await res.json();
        setActiveTask({ id: status.task_id, goal: status.goal, status: status.status });
        setLiveMetrics(prev => ({
          ...prev,
          completed_agents: status.completed_agents || [],
          current_agent:    status.current_agent    || null,
          error_count:      status.error_count      || 0,
          hitl_pending:     status.hitl_pending     || [],
          retry_counts:     status.retry_counts     || {},
          saga_log:         status.saga_log         || [],
          pei_violations:   status.pei_violations   || [],
          direct_reply:     status.direct_reply     || '',
          intent_type:      status.intent_type      || 'task',
        }));
        if (!['complete', 'failed'].includes(status.status)) {
          connectWs(status.task_id);
        }
        return;
      }
    } catch (_) {}
    setActiveTask({ id: session.task_id, goal: session.goal_preview, status: session.status });
  };

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
    } catch (e) {
      alert(`Failed to submit decision: ${e.message}`);
    } finally {
      setHitlLoading(false);
    }
  };

  const isTaskActive = activeTask && !['complete', 'failed'].includes(activeTask.status);
  const isConversation = liveMetrics.intent_type === 'conversation' || finalResult?.intent_type === 'conversation';
  const hasAgentWork = liveMetrics.completed_agents.length > 0 || liveMetrics.current_agent;
  const directReply = liveMetrics.direct_reply || finalResult?.summary || '';

  return (
    <div className="flex h-[100dvh] bg-white text-slate-800 font-sans">

      {/* ── Sidebar ────────────────────────────────────────────────────────── */}
      {sidebarOpen && (
        <div className="w-[260px] bg-[#f9f9f9] border-r border-[#e5e5e5] flex flex-col shrink-0">
          <div className="p-3 flex items-center justify-between">
            <button onClick={clearTask} className="flex items-center gap-2 px-3 py-2 text-sm font-medium hover:bg-slate-200/50 rounded-md text-slate-700">
              <div className="w-5 h-5 bg-white shadow-sm border border-slate-200 text-[#d97757] flex items-center justify-center rounded">
                <Blocks size={12} />
              </div>
              FRAME-MO
            </button>
            <button onClick={() => setSidebarOpen(false)} className="p-2 hover:bg-slate-200/50 rounded-md text-slate-500">
              <PanelLeftClose size={18} />
            </button>
          </div>

          <div className="px-3 pb-2 pt-1">
            <button onClick={clearTask} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-600 bg-white border border-slate-200 hover:bg-slate-50 rounded-md shadow-sm">
              <Plus size={16} /> New chat
            </button>
          </div>

          <div className="flex-1 overflow-y-auto mt-2 px-3">
            <div className="text-[11px] font-semibold text-slate-500 px-2 mb-2 tracking-wider">Recent Chats</div>
            {sessions.map((s) => (
              <button
                key={s.task_id}
                onClick={() => handleSessionClick(s)}
                className={`w-full text-left px-3 py-2 text-sm rounded-md transition-colors mb-0.5 ${activeTask?.id === s.task_id ? 'bg-[#ebebeb] font-medium' : 'hover:bg-[#ebebeb]'}`}
              >
                <p className="truncate text-slate-700 max-w-[200px]">{s.goal_preview}</p>
              </button>
            ))}
          </div>

          <div className="p-3 border-t border-[#e5e5e5] flex flex-col gap-1">
            <button onClick={() => setMcpModalOpen(true)} className="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 hover:bg-[#ebebeb] rounded-md">
              <Database size={16} /> MCP Config
            </button>
            <button className="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 hover:bg-[#ebebeb] rounded-md">
              <Settings size={16} /> Settings
            </button>
          </div>
        </div>
      )}

      {/* ── Main Chat Area ─────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col relative w-full h-full min-w-0">

        {!sidebarOpen && (
          <div className="absolute top-4 left-4 z-10">
            <button onClick={() => setSidebarOpen(true)} className="p-2 hover:bg-slate-100 rounded-md text-slate-500 bg-white/80 backdrop-blur border border-slate-200 shadow-sm">
              <PanelLeft size={18} />
            </button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto w-full">
          {!activeTask ? (
            <div className="h-full flex flex-col items-center justify-center px-4">
              <div className="w-12 h-12 bg-[#f4ece9] text-[#d97757] rounded-xl flex items-center justify-center mb-5">
                <Blocks size={24} />
              </div>
              <h1 className="text-2xl font-medium text-slate-800 mb-2">How can I help you today?</h1>
              <p className="text-slate-500 text-[15px] mb-8">Agentic web research, codebase queries, or automated communications.</p>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto w-full py-8 px-4 flex flex-col gap-6 pb-32">

              {/* User Message */}
              <div className="flex justify-end pt-4">
                <div className="bg-[#f3f4f6] text-slate-900 px-5 py-3.5 rounded-2xl rounded-tr-sm max-w-[85%] text-[15.5px] leading-relaxed break-words shadow-sm">
                  {activeTask.goal}
                </div>
              </div>

              {/* AI Response */}
              <div className="flex gap-4 items-start pb-8">
                <div className="w-8 h-8 rounded-full bg-[#d97757] text-white flex items-center justify-center shrink-0 mt-1 shadow-sm">
                  <Blocks size={16} />
                </div>

                <div className="flex-1 flex flex-col gap-3 min-w-0 pt-1.5">

                  {/* Tool Execution Block — only for task intent */}
                  {!isConversation && hasAgentWork && (
                    <div className="border border-slate-200 rounded-xl bg-white overflow-hidden shadow-sm max-w-[480px]">
                      <button
                        onClick={() => setAgentLogsExpanded(!agentLogsExpanded)}
                        className="w-full flex items-center gap-2 px-3 py-2.5 bg-slate-50/50 hover:bg-slate-100/50 text-slate-600 text-[13px] font-medium transition-colors"
                      >
                        {agentLogsExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <Workflow size={14} className="text-[#d97757]" />
                        {activeTask.status === 'complete' ? 'Used tools' : activeTask.status === 'rolling_back' ? 'Rolling back...' : 'Using tools...'}
                      </button>

                      {agentLogsExpanded && (
                        <div className="flex flex-col border-t border-slate-100 bg-white py-1">
                          {liveMetrics.completed_agents.map(ag => (
                            <AgentLogLine key={ag} agentId={ag} status="complete" retryCount={liveMetrics.retry_counts[ag]} />
                          ))}
                          {liveMetrics.current_agent && (
                            <AgentLogLine agentId={liveMetrics.current_agent} status="running" retryCount={liveMetrics.retry_counts[liveMetrics.current_agent]} />
                          )}
                          {!liveMetrics.current_agent && activeTask.status === 'running' && liveMetrics.completed_agents.length === 0 && (
                            <div className="flex items-center gap-2 text-sm py-2 px-3 text-slate-500">
                              <Loader2 size={14} className="animate-spin" /> Planning...
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Saga Rollback Indicator */}
                  {liveMetrics.saga_log.length > 0 && (
                    <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 max-w-[480px]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <Undo2 size={14} className="text-amber-600" />
                        <span className="text-[13px] font-semibold text-amber-800">Saga Rollback</span>
                      </div>
                      {liveMetrics.saga_log.map((entry, i) => (
                        <div key={i} className="text-xs text-amber-700 flex items-center gap-1.5 py-0.5">
                          {entry.success ? <CheckCircle2 size={11} className="text-green-500" /> : <XCircle size={11} className="text-red-500" />}
                          <span className="font-medium">{entry.agent}:</span> {entry.action}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* PEI Violations */}
                  {liveMetrics.pei_violations.length > 0 && (
                    <div className="bg-red-50 border border-red-200 rounded-xl p-3 max-w-[480px]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <ShieldAlert size={14} className="text-red-600" />
                        <span className="text-[13px] font-semibold text-red-800">Safety Monitor</span>
                      </div>
                      {liveMetrics.pei_violations.map((v, i) => (
                        <div key={i} className="text-xs text-red-700 py-0.5">
                          <span className="font-medium">{v.agent}:</span> {v.violation}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* HITL Approval */}
                  {liveMetrics.hitl_pending?.length > 0 && (
                    <div className="bg-amber-50/80 border border-amber-200/60 rounded-xl p-4 text-amber-900 max-w-full">
                      <div className="flex items-center gap-2 mb-2">
                        <AlertCircle size={16} className="text-amber-600" />
                        <h4 className="font-semibold text-sm">Action Needs Approval</h4>
                      </div>
                      {liveMetrics.hitl_pending.map((req, i) => (
                        <div key={i} className="text-xs font-mono bg-white border border-amber-100 p-2.5 rounded-lg mb-3">
                          <span className="font-semibold text-amber-700">{req.action?.tool || 'Tool'}</span>
                          <div className="text-slate-600 truncate mt-1">Input: {JSON.stringify(req.action?.input || {})}</div>
                        </div>
                      ))}
                      <div className="flex gap-2">
                        <button onClick={() => handleHITL(true)} disabled={hitlLoading} className="px-3 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-md text-[13px] font-medium transition disabled:opacity-50">Approve</button>
                        <button onClick={() => handleHITL(false)} disabled={hitlLoading} className="px-3 py-1.5 bg-white hover:bg-slate-50 border border-amber-200 text-amber-700 rounded-md text-[13px] font-medium transition disabled:opacity-50">Reject</button>
                      </div>
                    </div>
                  )}

                  {/* Final AI Response — works for BOTH conversation and task modes */}
                  {directReply && activeTask.status === 'complete' && (
                    <div className="prose prose-slate prose-sm sm:prose-base max-w-none text-slate-800 leading-relaxed mt-1">
                      <p className="whitespace-pre-wrap">{directReply}</p>
                      {finalResult?.highlights?.length > 0 && (
                        <ul className="mt-2 space-y-1 pl-4">
                          {finalResult.highlights.map((h, idx) => <li key={idx}>{h}</li>)}
                        </ul>
                      )}
                    </div>
                  )}

                  {/* Loading state for conversation */}
                  {isConversation && !directReply && activeTask.status !== 'complete' && (
                    <div className="flex items-center gap-2 text-slate-500 text-sm">
                      <Loader2 size={14} className="animate-spin" /> Thinking...
                    </div>
                  )}

                  {/* Error */}
                  {activeTask.status === 'failed' && (
                    <div className="text-red-600 text-[15px] flex items-center gap-2 mt-2">
                      <XCircle size={16} /> Error completing task.
                    </div>
                  )}
                </div>
              </div>
              <div ref={messagesEndRef} className="h-4" />
            </div>
          )}
        </div>

        {/* Input Bar */}
        <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-white via-white to-transparent pt-8 pb-6 px-4">
          <div className="max-w-3xl mx-auto">
            <div className={`relative flex flex-col bg-white rounded-2xl border shadow-sm transition-all focus-within:ring-2 focus-within:ring-[#d97757]/20 focus-within:border-[#d97757]/50 ${isTaskActive ? 'opacity-70 pointer-events-none border-slate-200' : 'border-slate-300'}`}>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
                }}
                disabled={isSubmitting || isTaskActive}
                placeholder={isTaskActive ? "Generating..." : "Message FRAME-MO..."}
                rows={2}
                className="w-full resize-none bg-transparent px-4 py-3.5 pr-12 text-[15px] leading-relaxed outline-none text-slate-800 placeholder-slate-400 disabled:opacity-50"
              />
              <div className="flex items-center justify-between px-3 pb-2.5">
                <button className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-md transition-colors">
                  <Paperclip size={18} />
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={!inputText.trim() || isSubmitting || isTaskActive}
                  className={`p-1.5 rounded-xl flex items-center justify-center transition-all ${
                    inputText.trim() && !isSubmitting && !isTaskActive
                      ? 'bg-black text-white shadow-sm'
                      : 'bg-slate-100 text-slate-400 cursor-not-allowed'
                  }`}
                >
                  {isSubmitting ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} strokeWidth={2.5} />}
                </button>
              </div>
            </div>
            <div className="text-center mt-2.5 text-xs text-slate-400 font-medium tracking-wide">
              FRAME-MO · Ollama orchestration · LTL verification · PEI monitoring · Saga recovery
            </div>
          </div>
        </div>
      </div>

      {/* ── MCP Modal ──────────────────────────────────────────────────────── */}
      {mcpModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-[400px] overflow-hidden">
            <div className="px-5 py-4 border-b border-slate-100 flex justify-between items-center">
              <div className="font-semibold text-[15px] flex items-center gap-2 text-slate-800">
                <Database size={18} className="text-slate-500" /> Context Protocols
              </div>
              <button onClick={() => setMcpModalOpen(false)} className="text-slate-400 hover:text-slate-600 hover:bg-slate-100 p-1 rounded-md">
                <XCircle size={20} />
              </button>
            </div>
            <div className="p-5 flex flex-col gap-3">
              {Object.keys(mcpHealth).length > 0 ? (
                Object.entries(mcpHealth).map(([srv, st]) => (
                  <div key={srv} className="flex justify-between items-center p-3 border border-slate-200 rounded-xl">
                    <span className="text-[14px] font-medium text-slate-700 capitalize">{srv.replace('-mcp', '')}</span>
                    <span className={`text-[11px] px-2.5 py-0.5 rounded-full font-semibold ${
                      st === 'healthy' ? 'bg-green-100 text-green-700' :
                      st === 'stub' ? 'bg-slate-100 text-slate-500' :
                      'bg-red-100 text-red-600'
                    }`}>{st}</span>
                  </div>
                ))
              ) : (
                <div className="text-sm text-slate-500 text-center py-6">Connecting...</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default App;
