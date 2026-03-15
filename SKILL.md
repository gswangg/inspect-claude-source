---
name: inspect-claude-source
description: Extract, prettify, and inspect Claude Code source from its Bun-compiled binary. Use when the user wants to understand Claude Code internals or find how a feature works.
allowed-tools: Read, Grep, Glob, Bash(python3 *), Bash(grep *), Bash(wc *), Bash(ls *), Bash(readlink *), Bash(basename *)
argument-hint: [search-term]
---

# Inspect Claude Code Source

Extract and search Claude Code's JavaScript source from its compiled binary. Works on macOS and Linux.

## Step 1: Determine current version

```bash
VERSION=$(basename "$(readlink ~/.local/bin/claude)")
```

## Step 2: Extract source (if not already done)

Check if `/tmp/claude-source/$VERSION/src/entrypoints/cli.js` exists. If not, run the extraction script bundled with this skill:

```bash
python3 ~/.claude/skills/inspect-claude-source/extract.py --text-only
```

The script auto-detects the platform and:
1. Finds the claude binary at `~/.local/bin/claude` and follows the symlink to get the version
2. Locates the Bun module data:
   - **macOS**: Uses `otool -l` to find the `__BUN/__bun` Mach-O section
   - **Linux**: Searches for the Bun trailer signature at EOF and parses the footer backwards
3. Parses the Bun StandaloneModuleGraph footer to find module boundaries
4. Extracts all embedded JS modules, strips bytecode prefixes
5. Formats with fast regex-based newline insertion (~250ms for 11MB)
6. Saves to `/tmp/claude-source/<version>/`
7. Skips if already extracted for this version

Options:
- `--text-only` - skip binary modules (.node, .wasm)
- `--no-format` - skip formatting step entirely
- `--pretty` - use prettier via bunx for full prettification (slower)
- `--binary PATH` - use a different binary path
- `--output-dir DIR` - change output directory (default: `/tmp/claude-source`)

## Step 3: Search or read the source

The main source file is `/tmp/claude-source/<version>/src/entrypoints/cli.js` (~435K lines formatted).

Other extracted modules:
- `image-processor.js` - Image processing addon wrapper
- `file-index.js` - File indexing addon wrapper
- `color-diff.js` - Color diff addon wrapper
- `audio-capture.js` - Audio capture addon wrapper

If the user provided a search term (`$ARGUMENTS`), search for it:

```bash
grep -n "$ARGUMENTS" /tmp/claude-source/$VERSION/src/entrypoints/cli.js | head -50
```

For broader exploration, use the Grep tool against `/tmp/claude-source/<version>/src/entrypoints/cli.js`.

## Tips for reading the source

- The source is minified then formatted. Variable names are short (single letters or short identifiers like `T`, `R`, `A`, `_`, `$`).
- Function names from external packages are preserved (e.g. `getUint32`, `randomUUID`).
- String literals are preserved verbatim — search for user-facing strings, CLI flag names, error messages, and event type names to find relevant code.
- The code uses CommonJS (`require`, `module.exports`) wrapped in Bun's CJS shim.
- Look for patterns like `type: "assistant"`, `type: "result"`, `type: "system"` to find event handling code.

## Binary structure reference

Claude Code is a Bun-compiled single-file executable (Mach-O on macOS, ELF on Linux):

```
[Binary headers + Bun runtime]
[Bun StandaloneModuleGraph data (appended at EOF on Linux, __BUN/__bun section on macOS)]
  [8-byte header]
  [modules data: paths + contents interleaved]
  [modules metadata: 52 bytes per module]
  [32-byte footer fields]
  [16-byte trailer: "\n---- Bun! ----\n"]
```

- **macOS**: Uses `otool -l` to find the `__BUN/__bun` Mach-O section offset and size
- **Linux**: Searches for the trailer at EOF, reads the 32-byte footer to get `offset_byte_count`, and computes section boundaries backwards

Both approaches are stable across versions since they rely on structural markers rather than hardcoded byte offsets.
