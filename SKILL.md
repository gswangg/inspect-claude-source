---
name: inspect-claude-source
description: Extract or stage Claude Code source from the installed CLI so you can inspect internals and reverse engineer features. Use when the user wants to understand how Claude Code works, trace a behavior, or find where a feature is implemented.
allowed-tools: Read, Grep, Glob, Bash(python3 *), Bash(grep *), Bash(wc *), Bash(ls *), Bash(readlink *), Bash(basename *), Bash(command -v *), Bash(find *)
argument-hint: [search-term]
---

# Inspect Claude Code Source

Extract or stage Claude Code's JavaScript source from the installed CLI. Works for both Bun-compiled builds and npm-installed `cli.js` bundles.

## Step 1: Determine current version

```bash
VERSION="$(python3 ~/.claude/skills/inspect-claude-source/extract.py --print-version)"
```

To see which Claude install is being inspected:

```bash
python3 ~/.claude/skills/inspect-claude-source/extract.py --print-binary
```

## Step 2: Extract or stage source (if not already done)

Check if `/tmp/claude-source/$VERSION/src/entrypoints/cli.js` exists. If not, run the extraction script bundled with this skill:

```bash
python3 ~/.claude/skills/inspect-claude-source/extract.py --text-only
```

The script auto-detects the install type and:
1. Resolves `claude` from `PATH` by default (or uses `--binary PATH`)
2. Detects the installed version from Claude Code package metadata when available
3. If Claude is a bundled `cli.js`, stages it directly to `/tmp/claude-source/<version>/src/entrypoints/cli.js`
4. If Claude is a Bun-compiled binary, locates the embedded Bun module graph and extracts modules
5. Formats the resulting JS for easier reading by default using fast newline insertion, or prettier with `--pretty`
6. Saves everything to `/tmp/claude-source/<version>/`
7. Skips work if that version is already present

Options:
- `--text-only` - skip binary modules (`.node`, `.wasm`) when extracting Bun builds
- `--pretty` - use prettier via `bunx` for full prettification (slower)
- `--binary PATH` - inspect a different Claude executable or `cli.js`
- `--output-dir DIR` - change output directory (default: `/tmp/claude-source`)
- `--print-version` - print the resolved Claude Code version and exit
- `--print-binary` - print the resolved Claude Code path and exit

## Step 3: Search or read the source

The main source file is:

```text
/tmp/claude-source/<version>/src/entrypoints/cli.js
```

If the user provided a search term (`$ARGUMENTS`), search for it:

```bash
grep -n "$ARGUMENTS" /tmp/claude-source/$VERSION/src/entrypoints/cli.js | head -50
```

For broader exploration, use Grep/Glob/Read against `/tmp/claude-source/<version>/`.

## Tips for reverse engineering Claude Code features

- Search for user-facing strings: command names, error text, help text, setting keys, event names.
- Look for protocol/event markers like `assistant`, `result`, `system`, `tool_use`, `stream`, `permission`.
- Search for CLI flags to find the command-line parsing path.
- Search for model/provider names to find API integration logic.
- Search for literal UI text to find TUI components and interaction flows.
- When you find a string match, read upward for schema definitions and downward for handlers.
- Expect minified/bundled code: variable names may be short, but string literals and many dependency function names survive.
