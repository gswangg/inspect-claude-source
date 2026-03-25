#!/usr/bin/env python3
"""Extract or stage Claude Code source for inspection.

Supports two install layouts:
1. Bun-compiled single-file executables (extract embedded modules)
2. npm-installed bundled cli.js files (stage the readable JS bundle directly)

Output:
    <output-dir>/<version>/src/entrypoints/cli.js
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

BUN_TRAILER = b"\n---- Bun! ----\n"
FALLBACK_BINARY = Path.home() / ".local" / "bin" / "claude"
DEFAULT_OUTPUT = Path("/tmp/claude-source")
PACKAGE_NAME = "@anthropic-ai/claude-code"


def default_binary() -> Path:
    which = shutil.which("claude")
    if which:
        return Path(which)
    return FALLBACK_BINARY


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def find_package_root(path: Path) -> Path | None:
    current = path.parent if path.is_file() else path
    while True:
        package_json = current / "package.json"
        if package_json.exists():
            data = read_json(package_json)
            if isinstance(data, dict) and data.get("name") == PACKAGE_NAME:
                return current
        if current.parent == current:
            return None
        current = current.parent


def find_version_from_header(path: Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                m = re.search(r"Version:\s*([0-9]+\.[0-9]+\.[0-9][^\s]*)", line)
                if m:
                    return m.group(1)
    except Exception:
        return None
    return None


def resolve_binary(binary_path: Path) -> tuple[Path, str, Path | None]:
    """Resolve the claude binary and extract its version."""
    resolved = binary_path.expanduser().resolve()
    if not resolved.exists():
        sys.exit(f"Binary not found: {binary_path}")

    package_root = find_package_root(resolved)
    if package_root:
        data = read_json(package_root / "package.json") or {}
        version = data.get("version")
        if version:
            return resolved, version, package_root

    header_version = find_version_from_header(resolved)
    if header_version:
        return resolved, header_version, package_root

    version = resolved.name
    return resolved, version, package_root


def is_probably_text_js(path: Path) -> bool:
    try:
        head = path.read_bytes()[:2048]
    except Exception:
        return False

    if BUN_TRAILER in head:
        return False
    if head.startswith(b"#!/usr/bin/env node"):
        return True
    if b"import " in head or b"require(" in head or b"// Version:" in head:
        return True

    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    return len(head) > 0 and printable / max(len(head), 1) > 0.95


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def stage_js_bundle(resolved: Path, out_dir: Path, package_root: Path | None) -> None:
    target = out_dir / "src" / "entrypoints" / "cli.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved, target)

    if package_root:
        for name in ("package.json", "README.md", "LICENSE.md", "sdk-tools.d.ts", "resvg.wasm"):
            copy_if_exists(package_root / name, out_dir / name)
        copy_if_exists(package_root / "vendor", out_dir / "vendor")
        (out_dir / "INSTALL_ROOT.txt").write_text(str(package_root) + "\n")
    else:
        (out_dir / "INSTALL_ROOT.txt").write_text(str(resolved.parent) + "\n")


def find_bun_section_macos(binary_path: Path) -> tuple[int, int]:
    """Find Bun section via otool (macOS Mach-O __BUN/__bun section)."""
    result = subprocess.run(
        ["otool", "-l", str(binary_path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit(f"otool failed: {result.stderr}")

    lines = result.stdout.split("\n")
    in_bun_seg = in_bun_sect = False
    offset = size = None

    for line in lines:
        s = line.strip()
        if "segname __BUN" in s:
            in_bun_seg = True
        elif in_bun_seg and "sectname __bun" in s:
            in_bun_sect = True
        elif in_bun_sect:
            if s.startswith("offset "):
                offset = int(s.split()[-1])
            elif s.startswith("size "):
                size = int(s.split()[-1], 16)
            if offset is not None and size is not None:
                return offset, size

    sys.exit("__BUN/__bun section not found in Mach-O binary")


def find_bun_section_linux(binary_path: Path) -> tuple[int, int]:
    """Find Bun section by searching for the trailer at EOF (Linux ELF)."""
    file_size = binary_path.stat().st_size

    with open(binary_path, "rb") as f:
        read_size = min(file_size, 1024)
        f.seek(file_size - read_size)
        tail = f.read(read_size)

        trailer_idx = tail.rfind(BUN_TRAILER)
        if trailer_idx < 0:
            sys.exit("Bun trailer not found in binary")

        trailer_abs = (file_size - read_size) + trailer_idx
        section_end = trailer_abs + len(BUN_TRAILER)

        f.seek(trailer_abs - 32)
        footer_data = f.read(32)

        offset_byte_count = struct.unpack_from("<I", footer_data, 0)[0]
        section_start = section_end - offset_byte_count - 48

    section_size = section_end - section_start
    return section_start, section_size


def find_bun_section(binary_path: Path) -> tuple[int, int]:
    """Find the Bun module data section, auto-detecting platform."""
    if platform.system() == "Darwin":
        return find_bun_section_macos(binary_path)
    return find_bun_section_linux(binary_path)


def parse_footer(section: bytes) -> dict:
    """Parse the Bun StandaloneModuleGraph footer from the section data."""
    section_size = len(section)

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
    i = 0
    while i < len(js) and js[i] < 32 and js[i] not in (9, 10, 13):
        i += 1
    return js[i:]


def fast_format(src: str) -> str:
    """Fast regex-based JS formatter. Adds newlines at statement boundaries."""
    src = re.sub(r";(?!\s*\n)", ";\n", src)
    src = re.sub(r"\{(?!\s*\n)", "{\n", src)
    src = re.sub(r"(?<!\n)\}", "\n}", src)
    src = re.sub(r"\}(?!\s*[\n;,).])", "}\n", src)
    return src


def prettify_prettier(file_path: Path) -> bool:
    """Run prettier on a JS file via bunx. Returns True on success."""
    for cmd in ("bunx", os.path.expanduser("~/.bun/bin/bunx")):
        try:
            result = subprocess.run(
                [cmd, "prettier", str(file_path), "--parser", "babel", "--write"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Extract and format Claude Code source from its installed CLI."
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=default_binary(),
        help=f"Path to claude executable or cli.js (default: {default_binary()})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--no-format",
        action="store_true",
        help="Skip formatting step entirely",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Use prettier for full prettification (slower, needs bunx)",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Only extract text modules (skip .node/.wasm binaries)",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print the resolved Claude Code version and exit",
    )
    parser.add_argument(
        "--print-binary",
        action="store_true",
        help="Print the resolved Claude Code path and exit",
    )
    args = parser.parse_args()

    t_start = time.time()

    resolved, version, package_root = resolve_binary(args.binary)

    if args.print_version:
        print(version)
        sys.exit(0)
    if args.print_binary:
        print(resolved)
        sys.exit(0)

    print(f"Binary: {resolved}")
    print(f"Version: {version}")
    print(f"Platform: {platform.system()}")

    out_dir = args.output_dir / version
    if out_dir.exists():
        print(f"Output already exists: {out_dir}")
        print("Remove it first to re-extract, or use a different --output-dir")
        sys.exit(0)

    if is_probably_text_js(resolved):
        print("Install type: bundled JavaScript (staging cli.js directly)")
        stage_js_bundle(resolved, out_dir, package_root)
        if not args.no_format:
            target = out_dir / "src" / "entrypoints" / "cli.js"
            if args.pretty:
                print("Prettifying cli.js with prettier...")
                if prettify_prettier(target):
                    print("  done")
                else:
                    print("  FAILED (prettier/bunx not found)")
            else:
                print("Formatting cli.js (fast mode)...")
                src = target.read_text(errors="replace")
                target.write_text(fast_format(src))
                print("  done")

        elapsed = time.time() - t_start
        print(f"\nStaged to: {out_dir}")
        print(f"Main source: {out_dir}/src/entrypoints/cli.js")
        print(f"Completed in {elapsed:.1f}s")
        sys.exit(0)

    sec_offset, sec_size = find_bun_section(resolved)
    print(f"Bun section: offset={sec_offset}, size={sec_size:,} bytes")

    with open(resolved, "rb") as f:
        f.seek(sec_offset)
        section = f.read(sec_size)

    footer = parse_footer(section)
    modules = extract_modules(section, footer)
    print(f"Found {len(modules)} modules")

    out_dir.mkdir(parents=True, exist_ok=True)
    text_modules = []

    for mod in modules:
        if args.text_only and not mod["is_text"]:
            continue

        path = mod["path"]
        contents = mod["contents"]

        if mod["is_text"]:
            contents = strip_bytecode_prefix(contents)

        if mod["is_text"] and not path.endswith(".js"):
            path = path + ".js"

        out_path = out_dir / path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(contents)

        kind = "text" if mod["is_text"] else "binary"
        print(f"  {path:40s} {len(contents):>12,} bytes  [{kind}]")

        if mod["is_text"]:
            text_modules.append(out_path)

    del section

    if not args.no_format and text_modules:
        if args.pretty:
            print("\nPrettifying JS modules with prettier...")
            for path in text_modules:
                print(f"  {path.name}...", end=" ", flush=True)
                if prettify_prettier(path):
                    print("done")
                else:
                    print("FAILED (prettier/bunx not found)")
                    break
        else:
            print(f"\nFormatting {len(text_modules)} modules (fast mode)...")
            for path in text_modules:
                src = path.read_text(errors="replace")
                formatted = fast_format(src)
                path.write_text(formatted)
            print("  done")

    elapsed = time.time() - t_start
    print(f"\nExtracted to: {out_dir}")
    print(f"Main source: {out_dir}/src/entrypoints/cli.js")
    print(f"Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
