import asyncio
import os
from tools.mcp_manager import _StubMCPConnection

# mock api key just for testing if dict expansion works
os.environ["TAVILY_API_KEY"] = "fake_key_for_test"

async def main():
    conn = _StubMCPConnection("tavily-mcp")
    args = {"args": {"query": "AI news today", "depth": "standard"}}
    
    # We expect a 401 unauthorized because key is fake, but NOT a 400 bad request,
    # meaning the payload is correctly shaped!
    res = await conn.call_tool("search", args)
    print("Search config parsed correctly if 401 error or no exception:", res)

asyncio.run(main())
