import asyncio
import json
from agents.code_agent import CodeAgent

async def test_json_recovery():
    # We mock _synthesize_from_tool_results and message.content 
    class MockAgent(CodeAgent):
        def _synthesize_from_tool_results(self, tr):
            return {"status": "success", "action_taken": "mock fallback synthesized"}
            
    agent = MockAgent()
    
    # Simulate problematic LLM output content with text outside JSON!
    content = """Now let me read the README.md file from the repository:
    {
       "repo": "Jivit87/TaskForgeAI",
       "action_taken": "read_file",
       "status": "failed",
       "details": "I cannot read."
    }"""
    
    # Re-use the agent's exact execution logic by manually mocking the loop.
    # We will just feed it directly to the JSON parser from lines 223+ in real base_agent.py
    try:
        import re
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            res = json.loads(match.group(0))
            print("Successfully regex extracted:", res)
        else:
            print("Failed to regex extract")
    except Exception as e:
        print("Crash:", e)

test_json_recovery()
