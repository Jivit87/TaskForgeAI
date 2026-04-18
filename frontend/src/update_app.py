import re

with open('App.jsx', 'r') as f:
    code = f.read()

# 1. User Message
code = code.replace(
    '<div className="bg-[#f3f4f6] text-slate-900 px-5 py-3.5 rounded-2xl rounded-tr-sm max-w-[85%] text-[15.5px] leading-relaxed break-words shadow-sm">',
    '<div className="bg-claude-msgUser text-claude-text px-5 py-3.5 rounded-2xl rounded-tr-md max-w-[85%] text-[15.5px] leading-relaxed break-words">'
)
code = code.replace(
    '<div className="flex justify-end">',
    '<div className="flex justify-end mb-4">'
)

# 2. AI Message Icons & Alignment
code = code.replace(
    '<div className="flex gap-4 items-start">',
    '<div className="flex gap-4 items-start mb-6">'
)
code = code.replace(
    '<div className="w-8 h-8 rounded-full bg-[#d97757] text-white flex items-center justify-center shrink-0 mt-1 shadow-sm">',
    '<div className="w-8 h-8 rounded-md bg-transparent text-claude-accent flex items-center justify-center shrink-0 mt-1">'
)
# change blocks size for AI icon
code = code.replace(
    '<Blocks size={16} />',
    '<Blocks size={20} strokeWidth={1.5} />'
)
code = code.replace(
    '<div className="flex-1 flex flex-col gap-3 min-w-0 pt-1.5">',
    '<div className="flex-1 flex flex-col gap-3 min-w-0 pt-1 text-claude-text">'
)

# 3. Main wrapper
code = code.replace(
    '<div className="flex h-[100dvh] bg-white text-slate-800 font-sans">',
    '<div className="flex h-[100dvh] bg-claude-bg text-claude-text font-serif selection:bg-claude-accent/30">'
)

# 4. Sidebar wrapper
code = code.replace(
    '<div className="w-[260px] bg-[#f9f9f9] border-r border-[#e5e5e5] flex flex-col shrink-0">',
    '<div className="w-[260px] bg-claude-sidebar border-r border-claude-border flex flex-col shrink-0">'
)
code = code.replace(
    'bg-white shadow-sm border-slate-200 text-[#d97757] font-medium',
    'bg-white shadow-sm border-claude-border text-claude-text font-medium'
)
code = code.replace(
    'hover:bg-black/5 text-slate-600',
    'hover:bg-claude-hover text-claude-subtext'
)
code = code.replace(
    '<div className="w-5 h-5 bg-white shadow-sm border border-slate-200 text-[#d97757] flex items-center justify-center rounded">',
    '<div className="w-5 h-5 bg-transparent text-claude-accent flex items-center justify-center rounded">'
)
code = code.replace(
    'text-[#d97757]',
    'text-claude-accent'
)
code = code.replace(
    'bg-slate-50/50 hover:bg-slate-100/50 text-slate-600',
    'bg-claude-hover/50 hover:bg-claude-hover text-claude-subtext'
)


# 5. Empty chat area
code = code.replace(
    '<div className="w-12 h-12 bg-[#f4ece9] text-[#d97757] rounded-xl flex items-center justify-center mb-5">',
    '<div className="w-12 h-12 bg-transparent text-claude-accent flex items-center justify-center mb-5">'
)

# 6. Center input wrapper gradient
code = code.replace(
    '<div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-white via-white to-transparent pt-8 pb-6 px-4">',
    '<div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-claude-bg via-claude-bg/90 to-transparent pt-12 pb-6 px-4">'
)

# 7. Input
code = code.replace(
    '`relative flex flex-col bg-white rounded-2xl border shadow-sm transition-all focus-within:ring-2 focus-within:ring-[#d97757]/20 focus-within:border-[#d97757]/50 ${isBlocked ? \'*\' : \'border-slate-300\'}`',
    '`relative flex flex-col bg-claude-msgUser rounded-2xl border border-claude-border shadow-sm transition-all focus-within:ring-2 focus-within:ring-claude-border focus-within:bg-white ${isBlocked ? \'opacity-60\' : \'\'}`'
)
# Note: dealing with backtick expressions using regex
code = re.sub(
    r'<div className={`relative flex flex-col bg-white rounded-2xl border shadow-sm transition-all focus-within:ring-2 focus-within:ring-\[#d97757\]/20 focus-within:border-\[#d97757\]/50 \$\{isBlocked \? \'opacity-60 border-slate-200\' : \'border-slate-300\'\}`} >'.replace(' ','\s*'),
    r'<div className={`relative flex flex-col bg-claude-msgUser rounded-2xl border border-claude-border shadow-sm transition-all focus-within:bg-white ${isBlocked ? "opacity-60" : ""}`}>',
    code
)

code = code.replace(
    'bg-black text-white shadow-sm hover:bg-slate-800',
    'bg-claude-accent text-white shadow-sm hover:opacity-90'
)

# 8. Markdown adjustments
code = code.replace(
    'className="prose prose-slate prose-sm sm:prose-base max-w-none text-slate-800 leading-relaxed mt-1"',
    'className="prose prose-slate prose-sm sm:prose-base max-w-none text-claude-text leading-relaxed mt-1 font-serif prose-p:leading-relaxed prose-pre:bg-claude-msgUser prose-pre:text-claude-text prose-pre:border prose-pre:border-claude-border prose-headings:font-sans"'
)

with open('App.jsx', 'w') as f:
    f.write(code)

print("Done")
