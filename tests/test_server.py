import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from telegram_cli_mcp.server import TelegramCLI, session_lock


FAKE_TG = '''\
import json
from pathlib import Path
import sys
import time

argv = sys.argv[1:]
assert argv[:1] == ["--config"]
assert argv[2:7] == ["--account", "existing-account", "--output", "json", "rpc"]
command, *args = argv[7:]
catalog = Path(argv[1])
if command == "--schema-cache":
    catalog = Path(args[0])
    command, *args = args[1:]
if command == "list":
    data = json.loads(catalog.read_text())
elif command == "describe":
    assert args[0] == "--"
    if args[1] == "oversize":
        sys.stdout.write("x" * (2 * 1024 * 1024))
        sys.exit(0)
    if args[1] == "hang":
        time.sleep(60)
    data = {"name": args[1], "parameters": []}
else:
    assert command == "invoke" and args[:2] == ["--yes", "--"]
    data = {"method": args[2], "params": json.loads(args[3]),
            "ids": [9007199254740991, 9007199254740993, -9223372036854775808]}
print(json.dumps({"schema": 1, "data": data}))
'''


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.binary = root / "fixture-tg"
        self.binary.write_text(f"#!{sys.executable}\n{FAKE_TG}")
        self.binary.chmod(0o700)
        self.config = root / "existing-config.json"
        self.config.write_text(json.dumps(["help.getConfig", "messages.getHistory"]))
        self.cli = TelegramCLI(str(self.binary), self.config, "existing-account",
                               root / "lock", frozenset({"help.getConfig"}))

    async def test_stdio_discovery_and_invocation(self):
        server = StdioServerParameters(command=sys.executable, args=[
            "-m", "telegram_cli_mcp.server", "--tg", str(self.binary),
            "--config", str(self.config), "--account", "existing-account",
            "--allow", "help.getConfig",
        ])
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self.assertEqual({tool.name for tool in (await session.list_tools()).tools},
                             {"telegram_list", "telegram_describe", "telegram_invoke"})

            result = await session.call_tool("telegram_list", {"limit": 1})
            self.assertEqual(result.structuredContent, {
                "methods": [{"name": "help.getConfig", "allowed": True}],
                "next_offset": 1, "total": 2,
            })
            self.config.write_text(json.dumps(["help.getConfig", "messages.futureMethod"]))
            result = await session.call_tool("telegram_list", {"query": "FUTURE"})
            self.assertEqual(result.structuredContent["methods"], [
                {"name": "messages.futureMethod", "allowed": False},
            ])

            described = await session.call_tool("telegram_describe", {"name": "inputPeerSelf"})
            self.assertEqual(described.structuredContent["name"], "inputPeerSelf")
            denied = await session.call_tool("telegram_invoke", {
                "method": "messages.futureMethod", "params": {},
            })
            self.assertTrue(denied.isError)
            self.assertIn("allowlist", denied.content[0].text)

            params = {"text": "--account=other é\n",
                      "nested": {"_": "inputPeerChannel", "access_hash": "9007199254740993"}}
            allowed = await session.call_tool("telegram_invoke", {
                "method": "help.getConfig", "params": params,
            })
            self.assertEqual(allowed.structuredContent, {
                "data": {"method": "help.getConfig", "params": params,
                         "ids": [9007199254740991, "9007199254740993", "-9223372036854775808"]},
            })

    async def test_lock_and_input_limit(self):
        with session_lock(self.cli.lock_file):
            with self.assertRaisesRegex(ToolError, "busy"):
                await self.cli.invoke("help.getConfig", {})
        with self.assertRaisesRegex(ToolError, "24,000"):
            await self.cli.invoke("help.getConfig", {"text": "x" * 24_000})
        with self.assertRaisesRegex(ToolError, "decimal strings"):
            await self.cli.invoke("help.getConfig", {"peer": {"access_hash": 9007199254740993}})
        self.assertEqual((await self.cli.invoke("help.getConfig", {}))["data"]["params"], {})

    async def test_auto_mode_invokes_new_methods_in_the_same_mcp_session(self):
        cache = self.config.with_name("runtime-schema")
        cache.write_text(json.dumps(["help.getConfig"]))
        server = StdioServerParameters(command=sys.executable, args=[
            "-m", "telegram_cli_mcp.server", "--tg", str(self.binary),
            "--config", str(self.config), "--account", "existing-account",
            "--schema-cache", str(cache), "--allow-all",
        ])
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            before = await session.call_tool("telegram_list", {"query": "future"})
            self.assertEqual(before.structuredContent["methods"], [])
            cache.write_text(json.dumps(["help.getConfig", "messages.futureMethod"]))
            listed = await session.call_tool("telegram_list", {"query": "future"})
            self.assertEqual(listed.structuredContent["methods"], [
                {"name": "messages.futureMethod", "allowed": True},
            ])
            called = await session.call_tool("telegram_invoke", {
                "method": "messages.futureMethod", "params": {},
            })
            self.assertFalse(called.isError)
            self.assertEqual(called.structuredContent["data"]["method"], "messages.futureMethod")

    def test_runtime_schema_cannot_be_mixed_with_per_method_access(self):
        with self.assertRaisesRegex(ValueError, "full method access"):
            TelegramCLI(str(self.binary), self.config, "existing-account",
                        self.cli.lock_file, frozenset({"invokeWithoutUpdates"}),
                        schema_cache=self.config.with_name("runtime-schema"))

    async def test_bounded_process_output_and_time(self):
        async with asyncio.timeout(5):
            with self.assertRaisesRegex(ToolError, "exceeded"):
                await self.cli.describe("oversize")
            with self.assertRaisesRegex(ToolError, "timed out"):
                await self.cli.run("describe", "--", "hang", timeout=0.4)
