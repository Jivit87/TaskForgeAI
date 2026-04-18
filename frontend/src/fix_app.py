with open("App.jsx", "r") as f:
    text = f.read()

# Fix layout wraps to authentic config limits (sans-serif, #f8f8f8)
text = text.replace(
    '<div className="flex h-[100dvh] bg-claude-bg text-claude-text font-serif selection:bg-claude-accent/30">',
    '<div className="flex h-[100dvh] bg-claude-bg text-claude-text font-sans antialiased selection:bg-slate-200">'
)

text = text.replace(
    '<div className="w-[260px] bg-claude-sidebar border-r border-claude-border flex flex-col shrink-0">',
    '<div className="w-[260px] bg-[#f3f3f3] border-r border-[#e5e5e5] flex flex-col shrink-0">'
)

# User bubble: shadow, border, font-sans
text = text.replace(
    '<div className="flex justify-end mb-4">\n        <div className="bg-claude-msgUser text-claude-text px-5 py-3.5 rounded-2xl rounded-tr-md max-w-[85%] text-[15.5px] leading-relaxed break-words">',
    '<div className="flex justify-end mb-5">\n        <div className="bg-white border text-[#2d2d2d] px-5 py-3.5 rounded-2xl max-w-[85%] text-[15px] font-sans leading-relaxed break-words shadow-sm">'
)

# AI bubbles padding
text = text.replace(
    '<div className="flex gap-4 items-start mb-6">\n      <div className="w-8 h-8 rounded-md bg-transparent text-claude-accent flex items-center justify-center shrink-0 mt-1">',
    '<div className="flex gap-4 items-start mb-6 font-sans">\n      <div className="w-7 h-7 bg-transparent text-claude-accent flex items-center justify-center shrink-0 mt-0.5">'
)

text = text.replace(
    '<div className="flex-1 flex flex-col gap-3 min-w-0 pt-1 text-claude-text">',
    '<div className="flex-1 flex flex-col gap-3 min-w-0 pt-0.5 text-[#2d2d2d]">'
)

# Tool execution background
text = text.replace(
    '<div className="border border-slate-200 rounded-xl bg-white overflow-hidden shadow-sm max-w-[480px]">',
    '<div className="border border-claude-border rounded-xl bg-[#f8f8f8] overflow-hidden max-w-[480px] shadow-sm">'
)

# Input area formatting
text = text.replace(
    '<div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-claude-bg via-claude-bg/90 to-transparent pt-12 pb-6 px-4">',
    '<div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-[#f8f8f8] via-[#f8f8f8]/95 to-transparent pt-12 pb-8 px-4">'
)
text = text.replace(
    '<div className={`relative flex flex-col bg-claude-msgUser rounded-2xl border border-claude-border shadow-sm transition-all focus-within:bg-white ${isBlocked ? "opacity-60" : ""}`}>',
    '<div className={`relative flex flex-col bg-white rounded-2xl border border-[#e5e5e5] shadow-sm transition-all focus-within:shadow-md focus-within:border-slate-300 ${isBlocked ? "opacity-60" : ""}`}>'
)

text = text.replace(
    'className="w-full resize-none bg-transparent px-4 py-3.5 pr-12 text-[15px] leading-relaxed outline-none text-slate-800 placeholder-slate-400 disabled:opacity-50"',
    'className="w-full resize-none bg-transparent px-4 py-3.5 pr-12 text-[15px] leading-relaxed outline-none text-[#2d2d2d] placeholder-[#767676] disabled:opacity-50"'
)

text = text.replace(
    'className="prose prose-slate prose-sm sm:prose-base max-w-none text-claude-text leading-relaxed mt-1 font-serif prose-p:leading-relaxed prose-pre:bg-claude-msgUser prose-pre:text-claude-text prose-pre:border prose-pre:border-claude-border prose-headings:font-sans"',
    'className="prose prose-slate prose-sm sm:prose-base max-w-none text-[#2d2d2d] leading-relaxed mt-1 font-sans prose-p:leading-relaxed prose-pre:bg-white prose-pre:text-[#2d2d2d] prose-pre:border prose-pre:border-[#e5e5e5] prose-headings:font-sans"'
)


with open("App.jsx", "w") as f:
    f.write(text)

