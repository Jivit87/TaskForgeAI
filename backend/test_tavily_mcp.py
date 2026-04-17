import asyncio
import os
import json

class MiniStdioMCPConnection:
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.proc = None
        self.msg_id = 0
        
    async def connect(self):
        env = {**os.environ, **self.config.get("env", {})}
        self.proc = await asyncio.create_subprocess_exec(
            self.config["command"], *self.config["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env
        )
        # Initialize
        self.msg_id += 1
        init_req = {"jsonrpc": "2.0", "id": self.msg_id, "method": "initialize", "params": {"clientInfo": {"name": "test", "version": "1.0"}, "protocolVersion": "2024-11-05", "capabilities": {}}}
        self.proc.stdin.write(json.dumps(init_req).encode() + b'\n')
        await self.proc.stdin.drain()
        
        while True:
            line = await self.proc.stdout.readline()
            if not line: raise Exception("MCP server closed unexpectedly")
            res = json.loads(line.decode())
            if res.get("id") == self.msg_id: break
            
        await self.proc.stdin.drain()
        # skip notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.proc.stdin.write(json.dumps(notif).encode() + b'\n')
        await self.proc.stdin.drain()
        
    async def call_tool(self, tool, args):
        self.msg_id += 1
        req_id = self.msg_id
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args
            }
        }
        self.proc.stdin.write(json.dumps(req).encode() + b'\n')
        await self.proc.stdin.drain()
        
        while True:
            line = await self.proc.stdout.readline()
            if not line: return {"error": "MCP closed"}
            res = json.loads(line.decode())
            if res.get("id") == req_id:
                if "error" in res: return {"error": res["error"]}
                texts = [c["text"] for c in res.get("result", {}).get("content", []) if c.get("type") == "text"]
                if not texts: return {"error": "No text content returned"}
                try:
                    return json.loads(texts[0])
                except Exception:
                    return {"content": texts[0]}
                    
    async def close(self):
        if self.proc:
            self.proc.terminate()
            await self.proc.wait()

async def main():
    config = {
        "command": "npx",
        "args": ["-y", "@tavily/mcp-server"],
        "env": {"TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", "")}
    }
    conn = MiniStdioMCPConnection("tavily-mcp", config)
    print("connecting...")
    await conn.connect()
    print("connected! calling tool...")
    res = await conn.call_tool("search", {"query": "test query", "depth": "basic"})
    print("result:", res)
    await conn.close()
    
asyncio.run(main())
