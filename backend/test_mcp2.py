import asyncio
from contextlib import AsyncExitStack
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters
import os

async def main():
    stack = AsyncExitStack()
    server_params = StdioServerParameters(command="npx", args=["-y", "@modelcontextprotocol/server-github"], env=os.environ)
    try:
        transport = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(*transport))
        await session.initialize()
        res = await session.call_tool("get_file_contents", arguments={"repo": "test/test", "path": "test"})
        print(type(res))
        print(res)
    except Exception as e:
        print("ERR:", e)
    finally:
        await stack.aclose()

asyncio.run(main())
