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

SCHEMA_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)?")
MAX_PARAMS = 24_000
MAX_OUTPUT = 1_048_576
MAX_SAFE_INTEGER = (1 << 53) - 1


def is_schema_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= 128 and bool(SCHEMA_NAME.fullmatch(name))


def mcp_json(value: Any, *, reject_wide_integers: bool = False) -> Any:
    if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
        if reject_wide_integers:
            raise ToolError("Pass integers outside the JavaScript safe range as decimal strings")
        return str(value)
    if isinstance(value, list):
        return [mcp_json(item, reject_wide_integers=reject_wide_integers) for item in value]
    if isinstance(value, dict):
        return {key: mcp_json(item, reject_wide_integers=reject_wide_integers)
                for key, item in value.items()}
    return value


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
    schema_cache: Path | None = None
    allow_all: bool = False
    linked_app_id: int | None = None

    def __post_init__(self) -> None:
        if self.schema_cache is not None and not self.allow_all:
            raise ValueError("Runtime schema mode requires full method access")
        if self.linked_app_id is not None and (
                self.schema_cache is None or not 0 < self.linked_app_id <= 2_147_483_647):
            raise ValueError("A positive linked app ID requires a runtime schema cache")

    async def run(self, command: str, *args: str, timeout: float) -> Any:
        schema = ["--schema-cache", str(self.schema_cache)] if self.schema_cache is not None else []
        if self.linked_app_id is not None:
            schema.extend(["--linked-app-id", str(self.linked_app_id)])
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, "--config", str(self.config), "--account", self.account,
                "--output", "json", "rpc", *schema, command, *args,
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
        try:
            return mcp_json(envelope["data"])
        except RecursionError:
            raise ToolError("tg JSON response is nested too deeply") from None

    async def list_methods(self, query: str, offset: int, limit: int) -> dict[str, Any]:
        names = await self.run("list", timeout=10)
        if not isinstance(names, list) or not all(is_schema_name(name) for name in names):
            raise ToolError("tg rpc list did not return a list of method names")
        matching = [name for name in names if query.casefold() in name.casefold()]
        return {
            "methods": [{"name": name, "allowed": self.allow_all or name in self.allowed}
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
        if not is_schema_name(method):
            raise ToolError("Expected a Telegram method name")
        if not self.allow_all and method not in self.allowed:
            raise ToolError("Telegram method is outside the configured allowlist")
        try:
            payload = json.dumps(params, allow_nan=False, separators=(",", ":"))
            if len(payload) > MAX_PARAMS:
                raise ToolError("Telegram RPC argument payload exceeds 24,000 bytes")
            mcp_json(params, reject_wide_integers=True)
        except (ValueError, RecursionError):
            raise ToolError("Telegram arguments must be valid JSON") from None
        with session_lock(self.lock_file):
            try:
                data = await self.run("invoke", "--yes", "--", method, payload, timeout=45)
            except ToolError as error:
                raise ToolError(f"{error}. Check Telegram before retrying a write.") from None
        return {"data": data}


def make_server(cli: TelegramCLI) -> FastMCP:
    server = FastMCP("telegram-cli", instructions=(
        "List Telegram methods with telegram_list, inspect inputs with telegram_describe, "
        "and call a configured method with telegram_invoke. "
        "Pass 64-bit IDs and access hashes as decimal strings to preserve precision."
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
        """Call an allowed Telegram method, which may send messages or change account data.

        Pass 64-bit IDs and access hashes as decimal strings.
        """
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
    parser.add_argument("--allow-all", action="store_true",
                        help="Let this MCP client invoke any method the configured CLI knows")
    parser.add_argument("--schema-cache", type=Path,
                        help="Use a runtime-capable tg and automatically refresh its published schema cache")
    parser.add_argument("--linked-app-id", type=int,
                        help="Public app ID shown by tg devices for the linked session")
    args = parser.parse_args()
    if not args.account.strip() or args.account.casefold() == "all":
        parser.error("--account must name one existing account")
    if not all(is_schema_name(name) for name in args.allow):
        parser.error("--allow requires a Telegram schema method name")
    if args.allow_all and args.allow:
        parser.error("Use --allow-all or --allow, not both")
    if args.schema_cache and not args.allow_all:
        parser.error("--schema-cache requires --allow-all")
    if args.linked_app_id is not None and (
            args.schema_cache is None or not 0 < args.linked_app_id <= 2_147_483_647):
        parser.error("--linked-app-id requires --schema-cache and a positive TL int")

    config = args.config.expanduser().resolve()
    lock_file = args.lock_file or config.with_name(".tg-session.lock")
    cli = TelegramCLI(args.tg, config, args.account, lock_file.expanduser().resolve(),
                      frozenset(args.allow),
                      args.schema_cache.expanduser().resolve() if args.schema_cache else None,
                      args.allow_all, args.linked_app_id)
    make_server(cli).run(transport="stdio")


if __name__ == "__main__":
    main()
