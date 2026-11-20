"""End-to-end smoke test for the MCP server (offline-friendly).

Spawns mcp_server/server.py over stdio, verifies the tool list, and calls
two tools (status + one catalog query). Requires the ``mcp`` package
(pip install -r requirements-dev.txt); skips gracefully when missing.

Run standalone:  python tests/test_mcp_server.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# UTF-8 stdout/stderr so emoji/unicode prints never crash on Windows cp1252
# consoles (same reconfigure app.py performs at startup — AGENTS.md gotcha #2).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_MIN_TOOLS = 10


async def _smoke() -> None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        print(f"SKIP: mcp package not installed ({exc}); "
              f"run: pip install -r requirements-dev.txt")
        return

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "mcp_server" / "server.py")],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"Tools ({len(names)}): {', '.join(names)}")
            assert len(names) >= EXPECTED_MIN_TOOLS, (
                f"expected >= {EXPECTED_MIN_TOOLS} tools, got {len(names)}: {names}"
            )
            assert "catalog_status" in names

            status = await session.call_tool("catalog_status", {})
            status_obj = json.loads(status.content[0].text)
            assert status_obj.get("success") is True, status_obj
            data = status_obj.get("data") or {}
            print(f"catalog_status → mode={data.get('mode')} "
                  f"available={data.get('available')} "
                  f"generatedAt={data.get('generatedAt')}")

            lookup = await session.call_tool(
                "lookup_table_impact", {"table_name": "FactSales"}
            )
            lookup_obj = json.loads(lookup.content[0].text)
            assert "success" in lookup_obj, lookup_obj  # well-formed envelope
            if lookup_obj.get("success"):
                print(f"lookup_table_impact → {lookup_obj.get('count')} match(es)")
            else:
                print(f"lookup_table_impact → {lookup_obj.get('error')}")

    print("OK")


def test_mcp_server_smoke() -> None:
    """pytest entry point (no async plugin needed)."""
    asyncio.run(_smoke())


if __name__ == "__main__":
    test_mcp_server_smoke()
