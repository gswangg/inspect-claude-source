# inspect-claude-source

Extract, prettify, and search [Claude Code](https://claude.ai/code)'s JavaScript source from its Bun-compiled binary.

Claude Code ships as a single Mach-O executable built with [Bun's single-file bundler](https://bun.sh/docs/bundler/executables). This tool parses the binary's embedded module graph, extracts the JS source files, and prettifies them so you can read, grep, and understand how Claude Code works internally.

## Purpose

- Explore Claude Code internals to understand how features are implemented
- Search for specific behaviors, event types, CLI flags, or error messages
- Works as a standalone script or as a [Claude Code skill](https://docs.anthropic.com/en/docs/claude-code/skills) (`/inspect-claude-source`)

## Prerequisites

- **macOS** (the binary is Mach-O; `otool` is used for section parsing)
- **Python 3.10+**
- **Claude Code** installed at `~/.local/bin/claude` (the default install location)
- **Bun** with `prettier` available via `bunx` (for prettification; optional with `--no-prettify`)

## Usage

### Standalone

```bash
python3 extract.py [OPTIONS]
```

### Options

| Flag | Description |
|---|---|
| `--text-only` | Skip binary modules (`.node`, `.wasm`) |
| `--no-prettify` | Skip prettier formatting (faster, but harder to read) |
| `--binary PATH` | Use a different binary path (default: `~/.local/bin/claude`) |
| `--output-dir DIR` | Change output directory (default: `/tmp/claude-source`) |

### Output

Files are extracted to `<output-dir>/<version>/`:

| File | Description |
|---|---|
| `claude.js` | Main source (~520K lines prettified) |
| `ripgrep.js` | Native ripgrep addon wrapper |
| `image-processor.js` | Image processing addon wrapper |
| `file-index.js` | File indexing addon wrapper |
| `color-diff.js` | Color diff addon wrapper |

### As a Claude Code skill

Copy the `SKILL.md` and `extract.py` to `~/.claude/skills/inspect-claude-source/`, then invoke with:

```
/inspect-claude-source [search-term]
```

Claude will extract the source (if needed), then search or explore based on your query.

## How it works

1. Follows the `~/.local/bin/claude` symlink to determine the installed version
2. Uses `otool -l` to locate the `__BUN/__bun` Mach-O section (no hardcoded offsets)
3. Parses the Bun `StandaloneModuleGraph` footer to find module boundaries
4. Extracts all embedded JS modules and strips bytecode prefixes
5. Prettifies with prettier via `bunx`
6. Saves to `/tmp/claude-source/<version>/` (skips re-extraction if already present)

### Binary structure

```
[Mach-O headers + Bun runtime]
[__BUN segment, __bun section]
  [8-byte header]
  [modules data: paths + contents interleaved]
  [modules metadata: 52 bytes per module]
  [32-byte footer fields]
  [16-byte trailer: "\n---- Bun! ----\n"]
```

## Limitations

- **macOS only.** The extractor uses `otool` and assumes a Mach-O binary. Linux builds use ELF and would need a different section-parsing approach.
- **Minified source.** Variable names are mangled (single letters like `T`, `R`, `A`). String literals and function names from dependencies are preserved, which makes searching by user-facing strings, flag names, and error messages effective.
- **No source maps.** The bundled source has no mapping back to the original TypeScript. You're reading the compiled output.
- **Version-coupled.** The extraction depends on Bun's `StandaloneModuleGraph` binary format. If Bun changes its embedding format, the footer parsing may need updating.
- **Large output.** The main `claude.js` file is ~520K lines prettified. Use grep/ripgrep rather than opening it in an editor.

## Tips for reading the source

- Search for **string literals** (error messages, CLI flag names, event type names like `"stream-json"`, `"assistant"`, `"result"`) to find relevant code paths.
- **Function names from dependencies** are preserved (e.g., `randomUUID`, `getUint32`, `structuredClone`).
- The code uses **CommonJS** (`require`, `module.exports`) wrapped in Bun's CJS shim.
- Look for Zod schemas (`y.object`, `y.literal`, `y.union`) to find type/event definitions.

## License

MIT
