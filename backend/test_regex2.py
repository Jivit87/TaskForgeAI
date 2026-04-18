import asyncio
import json
import re

content = """Now let me read the README.md file from the repository:
```json
{
   "repo": "Jivit87/TaskForgeAI",
   "action_taken": "read_file",
   "status": "failed",
   "details": "I cannot read."
}
```
"""

try:
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    parsed = json.loads(content.strip())
    print("Standard parse success:", parsed)
except json.JSONDecodeError as exc:
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            print("Regex parse success:", json.loads(match.group(0)))
        except:
            print("Regex failed")
