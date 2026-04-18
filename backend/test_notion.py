import asyncio
from tools.mcp_manager import MCPConnectionManager

async def test():
    m = MCPConnectionManager()
    await m.connect("notion-mcp")
    conn = m.connections.get("notion-mcp")
    if hasattr(conn, "list_tools"):
        res = await conn.list_tools()
        for t in res.tools:
            print(f"- {t.name}: {t.description}")
    await m.close_all()

if __name__ == "__main__":
    asyncio.run(test())
