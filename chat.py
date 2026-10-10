"""Ask the legacy ERP a question in plain language: Claude as MCP host, server.py as MCP server."""

import asyncio
import sys
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import Client, StdioServerParameters
from model_gateway import AsyncGateway

LEDGER = Path(__file__).with_name(".gateway") / "ledger.jsonl"


async def ask(question: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py"))])
    async with Client(server) as mcp_client:
        tools = (await mcp_client.list_tools()).tools
        # model-gateway picks the model (no route, so the large tier, with its server-side refusal fallback)
        # and writes what every turn of the loop cost to the ledger.
        gateway = AsyncGateway(AsyncAnthropic(), key="chat", ledger=LEDGER)
        runner = gateway.tool_runner(
            task="chat",
            max_iterations=15,
            output_config={"effort": "medium"},
            system=mcp_client.instructions or "",
            tools=[async_mcp_tool(tool, mcp_client.session) for tool in tools],
            messages=[{"role": "user", "content": question}],
        )
        async for message in runner:
            if message.stop_reason == "refusal":
                print("[the model declined this request]")
            for block in message.content:
                if block.type == "tool_use":
                    print(f"  -> {block.name}({block.input})")
                elif block.type == "text":
                    print(block.text)
        print(f"\n[{len(gateway.calls)} model calls, ${sum(turn['usd'] for turn in gateway.calls):.4f}]")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # piped output on Windows defaults to cp1252, which has no "≈"
    asyncio.run(ask(" ".join(sys.argv[1:]) or "Which three customers ordered the most in 2024, by revenue?"))
