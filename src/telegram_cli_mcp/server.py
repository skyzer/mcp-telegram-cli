"""Expose the installed tg CLI's RPC schema over MCP stdio."""

import argparse
import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

SCHEMA_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)?")
MAX_PARAMS = 24_000
MAX_OUTPUT = 1_048_576


def is_schema_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= 128 and bool(SCHEMA_NAME.fullmatch(name))


@contextmanager
def session_lock(path: Path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise ToolError("Could not open the configured Telegram session lock") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError("Telegram session is busy") from None
        except OSError:
            raise ToolError("Could not acquire the configured Telegram session lock") from None
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class TelegramCLI:
    binary: str
    config: Path
    account: str
    lock_file: Path
    allowed: frozenset[str]

    async def run(self, command: str, *args: str, timeout: float) -> Any:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, "--config", str(self.config), "--account", self.account,
                "--output", "json", "rpc", command, *args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise ToolError("Could not launch the configured tg binary") from None

        try:
            async with asyncio.timeout(timeout):
                try:
                    await process.stdout.readexactly(MAX_OUTPUT + 1)
                except asyncio.IncompleteReadError as response:
                    output = response.partial
                else:
                    raise ToolError("Telegram CLI output exceeded 1 MiB")
                if await process.wait() != 0:
                    raise ToolError(f"tg rpc {command} failed. Check the CLI in your terminal.")
        except TimeoutError:
            raise ToolError(f"tg rpc {command} timed out") from None
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.communicate()

        try:
            envelope = json.loads(output)
        except (ValueError, RecursionError):
            raise ToolError("tg did not return valid JSON") from None
        if (not isinstance(envelope, dict) or type(envelope.get("schema")) is not int
                or envelope["schema"] != 1 or "data" not in envelope):
            raise ToolError("Unsupported tg JSON envelope")
        return envelope["data"]

    async def list_methods(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        names = await self.run("list", timeout=10)
        if not isinstance(names, list) or not all(is_schema_name(name) for name in names):
            raise ToolError("tg rpc list did not return a list of method names")
        matching = [name for name in names if query.casefold() in name.casefold()]
        return {
            "methods": [{"name": name, "allowed": name in self.allowed}
                        for name in matching[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(matching) else None,
            "total": len(matching),
        }

    async def describe(self, name: str) -> dict[str, Any]:
        if not is_schema_name(name):
            raise ToolError("Invalid Telegram schema name")
        description = await self.run("describe", "--", name, timeout=10)
        if not isinstance(description, dict) or description.get("name") != name:
            raise ToolError("tg rpc describe did not return the requested schema description")
        return description

    async def invoke(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in self.allowed:
            raise ToolError("Telegram method is outside the configured allowlist")
        try:
            payload = json.dumps(params, allow_nan=False, separators=(",", ":"))
        except (ValueError, RecursionError):
            raise ToolError("Telegram arguments must be valid JSON") from None
        if len(payload) > MAX_PARAMS:
            raise ToolError("Telegram RPC argument payload exceeds 24,000 bytes")
        with session_lock(self.lock_file):
            try:
                data = await self.run("invoke", "--yes", "--", method, payload, timeout=45)
            except ToolError as error:
                raise ToolError(f"{error}. Check Telegram before retrying a write.") from None
        return {"data": data}


def make_server(cli: TelegramCLI) -> FastMCP:
    server = FastMCP("telegram-cli", instructions=(
        "List Telegram methods with telegram_list, inspect inputs with telegram_describe, "
        "and call a configured method with telegram_invoke."
    ))

    @server.tool()
    async def telegram_list(
        query: Annotated[str, Field(max_length=128)] = "",
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """Find callable methods in the installed Telegram schema and see which are allowed."""
        return await cli.list_methods(query, offset, limit)

    @server.tool()
    async def telegram_describe(name: str) -> dict[str, Any]:
        """Describe the TL fields of a Telegram method or constructor."""
        return await cli.describe(name)

    @server.tool()
    async def telegram_invoke(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call an allowed Telegram method. Some methods change the account or send messages."""
        return await cli.invoke(method, params)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tg", default="tg", help="RPC-capable tg executable or path")
    parser.add_argument("--config", required=True, type=Path, help="Existing tg configuration")
    parser.add_argument("--account", required=True, help="Existing account label")
    parser.add_argument("--lock-file", type=Path, help="Shared lock file used by cooperating callers")
    parser.add_argument("--allow", action="append", default=[], metavar="METHOD",
                        help="Allow this exact RPC name. Repeat for each method to enable.")
    args = parser.parse_args()
    if not args.account.strip() or args.account.casefold() == "all":
        parser.error("--account must name one existing account")
    if not all(is_schema_name(name) for name in args.allow):
        parser.error("--allow requires a Telegram schema method name")

    config = args.config.expanduser().resolve()
    lock_file = args.lock_file or config.with_name(".tg-session.lock")
    cli = TelegramCLI(args.tg, config, args.account, lock_file.expanduser().resolve(),
                      frozenset(args.allow))
    make_server(cli).run(transport="stdio")


if __name__ == "__main__":
    main()
