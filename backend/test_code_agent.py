import asyncio
from agents.code_agent import CodeAgent
from tools.mcp_manager import MCPConnectionManager

async def run_test():
    mcp = MCPConnectionManager()
    await mcp.connect("github-mcp")
    agent = CodeAgent(mcp_manager=mcp)
    
    # We want to peak at what tools it will inject into Ollama
    from tools.registry import get_groq_schemas
    
    tools = get_groq_schemas(agent.tool_names) if agent.tool_names else []
    known = set(t["function"]["name"] for t in tools)
    
    mcp_tools = await mcp.get_tools(agent.mcp_server)
    for t in mcp_tools:
        if t["function"]["name"] not in known:
            tools.append(t)
            
    print(f"Total tools accessible to agent: {len(tools)}")
    print("Tool names:")
    for t in tools:
        print(" -", t["function"]["name"])
        
    await mcp.close_all()

if __name__ == "__main__":
    asyncio.run(run_test())
