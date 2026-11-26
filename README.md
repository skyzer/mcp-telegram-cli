# Telegram CLI MCP

Give an MCP client access to Telegram through a `tg` session you already use. In automatic mode the CLI loads Telegram Desktop's published API schema at runtime. The same MCP tools can discover, describe, and invoke newly published methods after the schema refresh.

Telegram MCP servers such as [lexfrei/mcp-tg](https://github.com/lexfrei/mcp-tg) and [dryeab/mcp-telegram](https://github.com/dryeab/mcp-telegram) provide their own Telegram client and curated tools. Telegram CLI MCP focuses on session reuse and schema discovery. Its three MCP tools are stable even as the Telegram method catalog changes.

| MCP tool | What it does |
| --- | --- |
| `telegram_list` | Search the installed catalog by name. Results are paginated and indicate which methods may be invoked. |
| `telegram_describe` | Inspect the fields and types of a method or TL constructor. |
| `telegram_invoke` | Pass a method name and JSON arguments to `tg rpc invoke`. Choose full access or a fixed set of methods when starting the bridge. |

## Install

Use Python 3.11 or newer on macOS or Linux. The bridge currently requires the [schema RPC fork of `tg`](https://github.com/skyzer/cli/tree/a68c75683aacef377f638fed6fee68cc9ec3bc37). The standard `gotd/cli` 0.12.1 release lacks `tg rpc list`, `tg rpc describe`, and `tg rpc invoke`. Build the compatible binary into a separate path with Go 1.26 or newer.

```sh
git clone --branch codex/telegram-rpc-discovery --single-branch https://github.com/skyzer/cli.git tg-rpc-source
mkdir -p "$HOME/.local/bin"
cd tg-rpc-source
git switch --detach a68c75683aacef377f638fed6fee68cc9ec3bc37
go build -o "$HOME/.local/bin/tg-rpc" ./cmd/tg
```

Install the Python bridge in an isolated environment. The install targets an immutable GitHub release tag. The build backend and direct Python dependencies are pinned to the tested versions.

```sh
pipx install 'git+https://github.com/skyzer/mcp-telegram-cli.git@v0.1.0'
```

## Connect an MCP client

Register the bridge as one stdio MCP server. This example uses the local MCP JSON shape understood by Claude Desktop and Cursor. For a gateway or another client, use the same executable and argument list in its stdio configuration.

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/absolute/path/to/telegram-cli-mcp",
      "args": [
        "--tg", "/absolute/path/to/tg-rpc",
        "--config", "/absolute/path/to/existing/gotd.cli.yaml",
        "--account", "your-existing-account-label",
        "--allow", "help.getConfig"
      ]
    }
  }
}
```

Run the bridge with the same OS user, config, account label, and storage access as the existing CLI. That includes access to the macOS Keychain if `tg` uses it. `telegram_list` and `telegram_describe` work without a Telegram login. `telegram_invoke` needs the existing authenticated session. Remove the example `--allow` pair for discovery-only mode. Repeat `--allow` to grant more exact method names.

### Automatic schema updates and full method access

Build the runtime-schema version of the [CLI fork](https://github.com/skyzer/cli/tree/codex/telegram-runtime-schema). Use a released runtime-capable binary or check out the pinned source revision before building. The older RPC binary only knows its compiled schema.

```sh
telegram-cli-mcp \
  --tg "$HOME/.local/bin/tg-rpc" \
  --config "$HOME/.config/gotd/gotd.cli.yaml" \
  --account your-existing-account-label \
  --schema-cache "$HOME/.cache/telegram-cli-mcp/api.tl" \
  --allow-all
```

The CLI checks Telegram Desktop's published `api.tl` on the first request and at most once per five-minute freshness window while it is in use. It keeps the previous valid schema when the source is unavailable or serves an older layer. Methods become discoverable and callable in the existing MCP session after refresh. Telegram still decides which methods the account and live API layer can use.

`--allow-all` grants the full Telegram account's RPC authority. Use it on an authenticated owner route. Automatic mode requires full access because a raw TL method can wrap a nested RPC. Use the installed-schema mode with explicit `--allow` entries for a restricted client. With runtime TL inputs, omit flags words such as `flags`, omit false presence flags, and pass byte fields as base64.

Try `telegram_list` with `{"query":"help.getConfig"}`, then `telegram_describe` with `{"name":"help.getConfig"}`. To invoke it, pass `{"method":"help.getConfig","params":{}}`. When a field takes an abstract TL type, specify the constructor in `_`, for example `{"_":"inputPeerSelf"}`.

Pass 64-bit fields such as `access_hash` and `random_id` as decimal strings, for example `"9007199254740993"`. This preserves precision in JavaScript-based clients. The bridge rejects integer numbers outside JavaScript's safe range and returns large integers as decimal strings.

Each invocation takes a file lock. It defaults to `.tg-session.lock` next to the config, and `--lock-file` can point at a writable lock shared with another process. A lock only coordinates programs that use the same file. If an importer uses this session, configure its existing lock here or arrange for the processes to run at different times.

If you attach the bridge to a remote MCP gateway, have that gateway authenticate callers and authorize tools. Methods run with the Telegram account's privileges. Keep the `tg` configuration and session on the host.

## Develop

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests exercise the MCP tools with a disposable CLI fixture and require no Telegram credentials. Use `tg-rpc --output json rpc list` to check the real binary's catalog.
