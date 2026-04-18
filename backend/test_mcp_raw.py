import asyncio
from tools.mcp_manager import MCPConnectionManager

async def test():
    mcp = MCPConnectionManager()
    await mcp.connect("github-mcp")
    res = await mcp.call_tool("github-mcp", "get_file_contents", {"owner": "Jivit87", "repo": "TaskForgeAI", "path": ""}, idempotent=False)
    print(repr(res)[:1500])
    await mcp.close_all()

asyncio.run(test())
