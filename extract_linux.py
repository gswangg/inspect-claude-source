#!/usr/bin/env python3
"""Extract and prettify Claude Code source from its Bun-compiled ELF binary (Linux).

This is the Linux equivalent of extract.py, which uses otool (macOS only).
On Linux, we locate the Bun StandaloneModuleGraph by searching for the
trailer signature at the end of the binary, then parse backwards.

Usage:
    python3 extract_linux.py [--output-dir DIR] [--binary PATH]

Output:
    <output-dir>/<version>/claude.js          - main source (prettified)
    <output-dir>/<version>/ripgrep.js         - native addon wrapper
    <output-dir>/<version>/image-processor.js - native addon wrapper
    <output-dir>/<version>/...                - other embedded modules
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

BUN_TRAILER = b"\n---- Bun! ----\n"
DEFAULT_BINARY = Path.home() / ".local" / "bin" / "claude"
DEFAULT_OUTPUT = Path("/tmp/claude-source")


def resolve_binary(binary_path: Path) -> tuple[Path, str]:
    """Resolve the claude binary and extract its version."""
    resolved = binary_path.resolve()
    if not resolved.exists():
        sys.exit(f"Binary not found: {binary_path}")
    version = resolved.name
    return resolved, version


def find_bun_section(binary_path: Path) -> tuple[int, int]:
    """Find the Bun module data section by locating the trailer at EOF.

    On Linux ELF binaries, Bun appends the StandaloneModuleGraph data
    at the end of the file. We find the trailer, parse the footer to
    get offset_byte_count, and compute the section boundaries.

    Returns (section_start, section_size).
    """
    file_size = binary_path.stat().st_size

    with open(binary_path, "rb") as f:
        # Read the last 1KB to find the trailer
        read_size = min(file_size, 1024)
        f.seek(file_size - read_size)
        tail = f.read(read_size)

        trailer_idx = tail.rfind(BUN_TRAILER)
        if trailer_idx < 0:
            sys.exit("Bun trailer not found in binary")

        trailer_abs = (file_size - read_size) + trailer_idx
        section_end = trailer_abs + len(BUN_TRAILER)

        # Read the 32-byte footer just before the trailer
        f.seek(trailer_abs - 32)
        footer_data = f.read(32)

        offset_byte_count = struct.unpack_from("<I", footer_data, 0)[0]

        # Section starts at: section_end - offset_byte_count - 48
        # (48 = 32 bytes footer + 16 bytes trailer)
        section_start = section_end - offset_byte_count - 48

    section_size = section_end - section_start
    return section_start, section_size


def parse_footer(section: bytes) -> dict:
    """Parse the Bun StandaloneModuleGraph footer from the section data."""
    section_size = len(section)

    # Verify trailer at end of section
    trailer = section[section_size - len(BUN_TRAILER) :]
    assert trailer == BUN_TRAILER, f"Trailer mismatch: {trailer!r}"

    def u32(off_from_end: int) -> int:
        return struct.unpack_from("<I", section, section_size + off_from_end)[0]

    return {
        "offset_byte_count": u32(-48),
        "entrypoint_id": u32(-44),
        "modules_ptr_offset": u32(-40),
        "modules_ptr_length": u32(-36),
    }


def extract_modules(section: bytes, footer: dict) -> list[dict]:
    """Extract embedded modules from the section using footer metadata."""
    section_size = len(section)
    modules_start = section_size - (footer["offset_byte_count"] + 48)
    modules_end = modules_start + footer["modules_ptr_offset"]
    metadata_start = modules_end

    # Determine chunk size: try 52 first (modern format), then 28, 32
    modules_ptr_length = footer["modules_ptr_length"]
    chunk_size = None
    for cs in (52, 28, 32):
        if modules_ptr_length % cs == 0:
            chunk_size = cs
            break
    assert chunk_size is not None, (
        f"Cannot determine metadata chunk size (modulesPtrLength={modules_ptr_length})"
    )

    num_modules = modules_ptr_length // chunk_size
    modules = []

    for i in range(num_modules):
        meta_off = metadata_start + i * chunk_size

        path_off = struct.unpack_from("<I", section, meta_off)[0]
        path_len = struct.unpack_from("<I", section, meta_off + 4)[0]
        contents_off = struct.unpack_from("<I", section, meta_off + 8)[0]
        contents_len = struct.unpack_from("<I", section, meta_off + 12)[0]

        # Resolve path: scan backward from path_off for /$bunfs/root
        search_start = max(0, path_off - 50)
        chunk = section[search_start : path_off + path_len + 10]
        bunfs_idx = chunk.rfind(b"/$bunfs/root")

        if bunfs_idx >= 0:
            actual_start = search_start + bunfs_idx
            null_end = section.find(b"\x00", actual_start)
            if null_end > 0:
                path = section[actual_start:null_end].decode("utf-8", errors="replace")
            else:
                path = (
                    section[actual_start : actual_start + path_len + 20]
                    .split(b"\x00")[0]
                    .decode("utf-8", errors="replace")
                )
        else:
            path = section[path_off : path_off + path_len].decode(
                "utf-8", errors="replace"
            )

        # Strip bunfs root prefix
        for prefix in ("/$bunfs/root/", "/$bunfs/root"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        path = path.strip("/\n\t\x00")
        if not path:
            path = f"module_{i}.js"

        contents = section[contents_off : contents_off + contents_len]
        is_text = (
            len(contents) > 0
            and sum(1 for b in contents[:1000] if 32 <= b < 127 or b in (9, 10, 13))
            > 900
        )

        modules.append(
            {
                "index": i,
                "path": path,
                "contents": contents,
                "is_text": is_text,
            }
        )

    return modules


def strip_bytecode_prefix(contents: bytes) -> bytes:
    """Strip the Bun bytecode/CJS wrapper prefix to get clean JS source."""
    marker = b"(function(exports, require, module, __filename, __dirname) {"
    pos = contents.find(marker)
    if pos < 0:
        return contents

    js = contents[pos + len(marker) :]
    # Skip any leading non-printable bytes
    i = 0
    while i < len(js) and js[i] < 32 and js[i] not in (9, 10, 13):
        i += 1
    return js[i:]


def fast_format(src: str) -> str:
    """Fast regex-based JS formatter. Adds newlines at statement boundaries.

    No indentation, but puts each statement on its own line.
    ~250ms for 11MB vs ~3min for js-beautify on memory-constrained systems.
    """
    src = re.sub(r";(?!\s*\n)", ";\n", src)
    src = re.sub(r"\{(?!\s*\n)", "{\n", src)
    src = re.sub(r"(?<!\n)\}", "\n}", src)
    src = re.sub(r"\}(?!\s*[\n;,).])", "}\n", src)
    return src


def prettify_jsbeautify(file_paths: list[Path]) -> bool:
    """Prettify JS files in-place using js-beautify. Batches all files in one call."""
    if not file_paths:
        return True
    cmd = ["js-beautify", "-r"]
    for p in file_paths:
        cmd.extend(["-f", str(p)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Extract and prettify Claude Code source from its ELF binary (Linux)."
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=DEFAULT_BINARY,
        help=f"Path to claude binary (default: {DEFAULT_BINARY})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--no-prettify",
        action="store_true",
        help="Skip prettification step",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Use js-beautify for full prettification (slower, needs more memory)",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Only extract text modules (skip .node/.wasm binaries)",
    )
    args = parser.parse_args()

    t_start = time.time()

    # Resolve binary and version
    resolved, version = resolve_binary(args.binary)
    print(f"Binary: {resolved}")
    print(f"Version: {version}")

    # Output directory
    out_dir = args.output_dir / version
    if out_dir.exists():
        print(f"Output already exists: {out_dir}")
        print("Remove it first to re-extract, or use a different --output-dir")
        sys.exit(0)

    # Find the Bun section by trailer search (works on any platform)
    sec_offset, sec_size = find_bun_section(resolved)
    print(f"Bun section: offset={sec_offset}, size={sec_size:,} bytes")

    with open(resolved, "rb") as f:
        f.seek(sec_offset)
        section = f.read(sec_size)

    # Parse footer and extract modules
    footer = parse_footer(section)
    modules = extract_modules(section, footer)
    print(f"Found {len(modules)} modules")

    # Write modules to disk
    out_dir.mkdir(parents=True, exist_ok=True)
    text_modules = []

    for mod in modules:
        if args.text_only and not mod["is_text"]:
            continue

        path = mod["path"]
        contents = mod["contents"]

        # Strip bytecode prefix from text modules
        if mod["is_text"]:
            contents = strip_bytecode_prefix(contents)

        # Use the basename for output (flatten paths)
        out_name = os.path.basename(path)
        if mod["is_text"] and not out_name.endswith(".js"):
            out_name = out_name + ".js"

        # Main source file gets a canonical name
        if "cli" in path and path.endswith(".js"):
            out_name = "claude.js"

        out_path = out_dir / out_name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(contents)

        kind = "text" if mod["is_text"] else "binary"
        print(f"  {out_name:40s} {len(contents):>12,} bytes  [{kind}]")

        if mod["is_text"]:
            text_modules.append(out_path)

    # Free section memory before prettification
    del section

    # Prettify text modules
    if not args.no_prettify and text_modules:
        if args.pretty:
            # Full prettification with js-beautify (slower, ~3min for 11MB)
            names = ", ".join(p.name for p in text_modules)
            print(f"\nPrettifying {len(text_modules)} modules with js-beautify ({names})...")
            if prettify_jsbeautify(text_modules):
                print("  done")
            else:
                print("  FAILED (js-beautify not found — install with: npm i -g js-beautify)")
        else:
            # Fast formatting: regex-based newline insertion (~250ms for 11MB)
            print(f"\nFormatting {len(text_modules)} modules (fast mode)...")
            for path in text_modules:
                src = path.read_text(errors="replace")
                formatted = fast_format(src)
                path.write_text(formatted)
            print("  done")

    elapsed = time.time() - t_start
    print(f"\nExtracted to: {out_dir}")
    print(f"Main source: {out_dir}/claude.js")
    print(f"Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
