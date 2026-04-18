import asyncio
from contextlib import AsyncExitStack
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp import ClientSession, StdioServerParameters

async def main():
    stack = AsyncExitStack()
    try:
        print(dir(sse_client))
        print(dir(ClientSession))
    finally:
        await stack.aclose()

asyncio.run(main())
