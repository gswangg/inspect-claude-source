# inspect-claude-source

Extract or stage [Claude Code](https://claude.ai/code)'s JavaScript source from the installed CLI so you can inspect internals and reverse engineer features.

This works across common install layouts:
- **Bun-compiled single-file builds**: extract embedded modules from the binary
- **npm-installed bundles**: stage the installed `cli.js` directly into a stable versioned path

## Purpose

- Explore Claude Code internals to understand how features are implemented
- Trace specific behaviors, event types, CLI flags, settings, prompts, or error messages
- Build a reusable reverse-engineering workflow for future Claude Code work
- Use as a Claude Code skill via `/inspect-claude-source`

## Prerequisites

- **Python 3.10+**
- **Claude Code** installed and reachable via `claude` in `PATH` (or pass `--binary PATH`)
- **Bun** with `prettier` available via `bunx` for `--pretty` formatting (optional)

## Usage

### Standalone

```bash
python3 extract.py [OPTIONS]
```

### Options

| Flag | Description |
|---|---|
| `--text-only` | Skip binary modules (`.node`, `.wasm`) when extracting Bun builds |
| `--pretty` | Use prettier via `bunx` for full prettification instead of fast newline formatting |
| `--binary PATH` | Use a different Claude executable or `cli.js` |
| `--output-dir DIR` | Change output directory (default: `/tmp/claude-source`) |
| `--print-version` | Print the resolved Claude Code version and exit |
| `--print-binary` | Print the resolved Claude Code path and exit |

## Output

Files are written to:

```text
<output-dir>/<version>/
```

Primary file:

```text
<output-dir>/<version>/src/entrypoints/cli.js
```

For npm-installed bundles, the staged output may also include:
- `package.json`
- `README.md`
- `LICENSE.md`
- `sdk-tools.d.ts`
- `vendor/`
- `INSTALL_ROOT.txt`

For Bun-compiled builds, extracted modules are written under their embedded paths.

## As a Claude Code skill

Copy this directory to:

```bash
~/.claude/skills/inspect-claude-source/
```

Then invoke:

```text
/inspect-claude-source [search-term]
```

Claude will resolve the current install, extract or stage the source if needed, and then search/read it.

## How it works

1. Resolves `claude` from `PATH` by default
2. Detects the Claude Code version from package metadata when possible
3. If the install is a readable `cli.js` bundle, stages it directly to `/tmp/claude-source/<version>/src/entrypoints/cli.js`
4. If the install is a Bun-compiled binary, parses the embedded `StandaloneModuleGraph` and extracts modules
5. Formats the resulting JS for easier reading by default using fast newline insertion, or prettier with `--pretty`
6. Reuses the staged output on later runs

## Reverse-engineering tips

- Search **string literals** first: user-visible messages, flag names, event types, config keys.
- Search for protocol words like `assistant`, `result`, `tool_use`, `permission`, `stream`.
- Use **grep/ripgrep** instead of opening huge files directly.
- Once you find a hit, read around it to find parser logic, schemas, and handlers.
- Expect bundled/minified code: identifiers may be short, but string literals are still your map.

## License

MIT
