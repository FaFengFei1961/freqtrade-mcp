"""End-to-end MCP server smoke test.

Spawns yangyang-mcp via stdio, runs the MCP handshake, lists tools, and
calls a read-only tool to verify the bridge to Freqtrade works.

Run:
    .venv/Scripts/python.exe tests/test_mcp_e2e.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


YANGYANG_EXE = (
    Path(__file__).resolve().parent.parent
    / ".venv"
    / "Scripts"
    / "yangyang.exe"
)


async def main() -> int:
    if not YANGYANG_EXE.exists():
        print(f"ERROR: {YANGYANG_EXE} not found.", file=sys.stderr)
        return 1

    params = StdioServerParameters(
        command=str(YANGYANG_EXE),
        args=["mcp"],
        env=None,
    )

    print("[1] Spawning yangyang-mcp ...")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("[2] Initializing MCP session ...")
            init = await session.initialize()
            print(f"    server: {init.serverInfo.name} v{init.serverInfo.version}")

            print("[3] Listing tools ...")
            tools = await session.list_tools()
            print(f"    {len(tools.tools)} tools registered:")
            for t in tools.tools:
                print(f"      - {t.name}")

            print("[4] Listing prompts ...")
            prompts = await session.list_prompts()
            print(f"    {len(prompts.prompts)} prompts registered:")
            for p in prompts.prompts:
                print(f"      - {p.name}")

            print("[5] Calling get_config_summary() ...")
            result = await session.call_tool("get_config_summary", {})
            text = result.content[0].text if result.content else "(empty)"
            print("    " + text.replace("\n", "\n    ")[:800])

            print("\n[6] Calling get_balance() ...")
            result = await session.call_tool("get_balance", {})
            text = result.content[0].text if result.content else "(empty)"
            print("    " + text.replace("\n", "\n    ")[:500])

            print("\n[7] Calling get_whitelist() ...")
            result = await session.call_tool("get_whitelist", {})
            text = result.content[0].text if result.content else "(empty)"
            print("    " + text.replace("\n", "\n    ")[:400])

            print("\n[8] Calling get_pair_data(SOL/USDT:USDT, 1h, 5) ...")
            result = await session.call_tool(
                "get_pair_data",
                {"pair": "SOL/USDT:USDT", "timeframe": "1h", "limit": 5},
            )
            text = result.content[0].text if result.content else "(empty)"
            print("    " + text.replace("\n", "\n    ")[:600])

            print("\n[OK] End-to-end MCP smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
