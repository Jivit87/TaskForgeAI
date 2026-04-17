import React, { useState } from 'react';
import { 
  Plus, Search, User, Paperclip, ArrowUp, PanelLeftClose, PanelLeft, Database, Blocks, Settings
} from 'lucide-react';

const App = () => {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [mcpModalOpen, setMcpModalOpen] = useState(false);
  const [inputText, setInputText] = useState("");

  const recentChats = [
    "Research AI trends and email summary",
    "Update Notion with project specs",
    "Create GitHub issue for the bug",
  ];

  return (
    <div className="flex h-screen bg-claude-bg font-sans text-claude-text">
      {/* Sidebar */}
      {sidebarOpen && (
        <div className="w-[260px] bg-claude-sidebar border-r border-claude-border flex flex-col transition-all duration-300 flex-shrink-0">
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

          <div className="px-3 pb-3 mt-1">
            <button className="w-full flex items-center gap-2 px-3 py-2 bg-white hover:bg-white text-sm rounded-md border border-claude-border shadow-sm transition-all focus:outline-none focus:ring-1 focus:ring-black/10">
              <Plus size={16} className="text-claude-subtext" />
              New chat
            </button>
          </div>

          <div className="flex-1 overflow-y-auto mt-2 px-3">
            <div className="text-xs font-semibold text-claude-subtext px-3 mb-2 uppercase tracking-wide">Recent</div>
            {recentChats.map((chat, idx) => (
              <button key={idx} className="w-full text-left truncate px-3 py-2 text-sm hover:bg-black/5 rounded-md text-claude-text">
                {chat}
              </button>
            ))}
          </div>

          <div className="p-3 border-t border-claude-border">
            <button className="w-full flex items-center gap-2 px-3 py-2 hover:bg-black/5 rounded-md text-sm text-claude-text">
              <Settings size={16} className="text-claude-subtext" />
              Settings
            </button>
            <button className="w-full flex items-center gap-2 px-3 py-2 hover:bg-black/5 rounded-md text-sm text-claude-text mt-1">
              <User size={16} className="text-claude-subtext" />
              User Profile
            </button>
          </div>
        </div>
      )}

      {/* Main Content */}
      <div className="flex-1 flex flex-col h-screen min-w-0 bg-white">
        {/* Top Header */}
        <div className="h-14 flex items-center justify-between px-4">
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
            <div className="px-2 py-1 bg-green-50 text-green-700 text-xs font-medium rounded-md flex items-center gap-1.5 border border-green-200">
              <div className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
              SYSTEM ONLINE
            </div>
          </div>
        </div>

        {/* Chat Area - Splash screen for new chat */}
        <div className="flex-1 overflow-y-auto flex flex-col relative w-full items-center justify-center">
             <div className="text-center max-w-lg px-4 flex flex-col items-center">
                 <div className="w-14 h-14 bg-[#d97757] text-white rounded-2xl flex items-center justify-center mb-6 shadow-sm">
                   <Blocks size={28} />
                 </div>
                 <h1 className="text-[32px] font-medium text-claude-text mb-4 tracking-tight">Good morning.</h1>
                 <p className="text-claude-subtext text-center text-[15px] leading-relaxed">
                   I am FRAME-MO, a multi-orchestral agent framework. What task would you like to build, research, or automate today?
                 </p>
             </div>
        </div>

        {/* Input Area */}
        <div className="w-full max-w-3xl mx-auto px-4 pb-6 pt-2">
          <div className="relative flex flex-col rounded-2xl border border-claude-border bg-[#f1f1f1]/50 shadow-sm overflow-hidden focus-within:ring-1 focus-within:ring-black/10 focus-within:border-black/20 focus-within:bg-white transition-all duration-200">
            
            <textarea 
              rows={3}
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              placeholder="Message FRAME-MO..."
              className="w-full resize-none bg-transparent px-4 py-4 pr-12 text-[15px] outline-none text-claude-text placeholder-claude-subtext"
            />

            <div className="flex items-center justify-between px-3 pb-3">
              <div className="flex items-center gap-1.5">
                 <button className="p-1.5 text-claude-subtext hover:text-claude-text rounded-md hover:bg-black/5 transition-colors">
                    <Paperclip size={18} />
                 </button>
                 
                 {/* MCP Add Feature */}
                 <button 
                  onClick={() => setMcpModalOpen(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1 text-claude-subtext hover:text-claude-text rounded-md hover:bg-black/5 transition-colors border border-transparent hover:border-black/10"
                  title="Add MCP context or connection"
                 >
                   <Database size={15} />
                   <span className="text-sm font-medium">Add MCP</span>
                 </button>
              </div>

              <button 
                className={`p-2 rounded-xl flex items-center justify-center transition-colors ${
                  inputText.trim() ? "bg-[#d97757] text-white hover:bg-[#c96a4b]" : "bg-black/5 text-black/20 cursor-not-allowed"
                }`}
              >
                <ArrowUp size={18} strokeWidth={2.5} />
              </button>
            </div>
          </div>
          <div className="text-center mt-3 text-[13px] text-claude-subtext">
            FRAME-MO can make task executions. Verify outputs and irreversible actions inside dashboard.
          </div>
        </div>
      </div>

      {/* MCP Modal */}
      {mcpModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-[420px] overflow-hidden flex flex-col animate-in fade-in zoom-in-95 duration-200">
            <div className="px-5 py-4 border-b border-claude-border flex justify-between items-center bg-white">
              <div className="flex items-center gap-2 font-medium text-[15px]">
                <Database size={18} className="text-[#d97757]" />
                Add MCP Connection
              </div>
              <button onClick={() => setMcpModalOpen(false)} className="text-claude-subtext hover:text-claude-text p-1 hover:bg-black/5 rounded-md">
                 &times;
              </button>
            </div>
            
            <div className="p-5 flex flex-col gap-4">
              <p className="text-sm text-claude-subtext">
                Connect external providers to give FRAME-MO direct access to live context and tools.
              </p>
              
              <div className="flex flex-col gap-3">
                 <button className="flex items-center gap-3 p-3 border border-claude-border rounded-xl hover:border-[#d97757] hover:bg-[#d97757]/5 transition-colors text-left group">
                    <div className="w-8 h-8 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center flex-shrink-0">
                       <Search size={16} />
                    </div>
                    <div>
                      <div className="font-medium text-[15px] group-hover:text-[#d97757]">Tavily Web Search</div>
                      <div className="text-[13px] text-claude-subtext mt-0.5">Live web reasoning</div>
                    </div>
                    <div className="ml-auto text-[#d97757] opacity-0 group-hover:opacity-100 transition-opacity">
                      <Plus size={18} />
                    </div>
                 </button>

                 <button className="flex items-center gap-3 p-3 border border-claude-border rounded-xl hover:border-[#d97757] hover:bg-[#d97757]/5 transition-colors text-left group">
                    <div className="w-8 h-8 rounded-full bg-slate-100 text-slate-800 flex items-center justify-center flex-shrink-0">
                       <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4"/><path d="M9 18c-4.51 2-5-2-7-2"/></svg>
                    </div>
                    <div>
                      <div className="font-medium text-[15px] group-hover:text-[#d97757]">GitHub MCP</div>
                      <div className="text-[13px] text-claude-subtext mt-0.5">Repo read/write access</div>
                    </div>
                    <div className="ml-auto text-[#d97757] opacity-0 group-hover:opacity-100 transition-opacity">
                      <Plus size={18} />
                    </div>
                 </button>
                 
                 <button className="flex items-center gap-3 p-3 border border-claude-border rounded-xl hover:border-[#d97757] hover:bg-[#d97757]/5 transition-colors text-left group">
                    <div className="w-8 h-8 rounded-full bg-slate-100 text-black flex items-center justify-center flex-shrink-0">
                       <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M4.459 4.208c.746-.606 1.776-.796 3.125-.572l12.7 2.052c1.077.172 1.536.836 1.492 1.731l-.222 4.417c2.398-1.637 1.895-3.056-.474-3.414l-11.758-1.745c-.886-.131-2.164.088-3.033.454l-1.83.743-1.045 4.908c-.732-2.193.308-3.955 1.045-8.574Zm-2.316 6.84c-.443-2.155.626-3.003 2.502-3.327l14.1-2.42c1.773-.303 2.91.433 3.352 2.584l2.062 10.02c.443 2.154-.627 3.002-2.5 3.326L7.561 23.65c-1.774.304-2.909-.432-3.352-2.583L2.143 11.048Zm5.78 4.22c.11 1.751 1.63 2.96 3.342 2.666l5.053-.865c1.713-.294 3.023-1.928 2.91-3.674-.112-1.752-1.63-2.962-3.344-2.668l-5.053.865c-1.712.293-3.022 1.928-2.909 3.676Z"/></svg>
                    </div>
                    <div>
                      <div className="font-medium text-[15px] group-hover:text-[#d97757]">Notion MCP</div>
                      <div className="text-[13px] text-claude-subtext mt-0.5">Workspace knowledge</div>
                    </div>
                    <div className="ml-auto text-[#d97757] opacity-0 group-hover:opacity-100 transition-opacity">
                      <Plus size={18} />
                    </div>
                 </button>
              </div>
            </div>
            
          </div>
        </div>
      )}
    </div>
  );
};

export default App;
