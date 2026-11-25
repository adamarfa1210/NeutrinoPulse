"""Symbol resolution via ntox86_64-addr2line and QNX client/server code context pairing."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

LINE_RE = re.compile(r"^(.*):(\d+)(?:\s+\(discriminator.*\))?$")
HEX_IP_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_SOURCE_EXTS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"}
_SKIP_DIRS = {".git", "__pycache__", "build", "CMakeFiles", ".venv", "venv"}


def is_elf_file(path: Path) -> bool:
    """Return True if path is a regular ELF binary."""
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def iter_source_files(target_dir: Path) -> list[Path]:
    """Recursively list C/C++ source and header files under target_dir."""
    if not target_dir.is_dir():
        return []
    return sorted(
        p for p in target_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in _SOURCE_EXTS
        and not any(part in _SKIP_DIRS for part in p.relative_to(target_dir).parts)
    )


def compute_cache_stamp(binary_path: str, target_dir: str) -> tuple[int, int]:
    """Compute (binary_mtime_ns, max_source_mtime_ns) to invalidate cache on recompiles."""
    try:
        bin_mtime = Path(binary_path).stat().st_mtime_ns
    except OSError:
        bin_mtime = 0
    src_mtime = 0
    for src in iter_source_files(Path(target_dir)):
        try:
            mt = src.stat().st_mtime_ns
            if mt > src_mtime:
                src_mtime = mt
        except OSError:
            pass
    return (bin_mtime, src_mtime)


def find_addr2line() -> str:
    """Auto-detect QNX ntox86_64-addr2line across Windows, Linux, and macOS hosts."""
    names = ("ntox86_64-addr2line.exe", "ntox86_64-addr2line") if os.name == "nt" else ("ntox86_64-addr2line",)
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit

    candidates: list[Path] = []
    qnx_host = os.getenv("QNX_HOST", "").strip()
    if qnx_host:
        candidates.extend(Path(qnx_host) / "usr" / "bin" / name for name in names)
    candidates.extend([
        Path(r"C:\qnx800\host\win64\x86_64\usr\bin\ntox86_64-addr2line.exe"),
        Path("/opt/qnx800/host/linux/x86_64/usr/bin/ntox86_64-addr2line"),
        Path.home() / "qnx800" / "host" / "linux" / "x86_64" / "usr" / "bin" / "ntox86_64-addr2line",
    ])
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return str(candidates[0])


DEFAULT_ADDR2LINE = find_addr2line()


def resolve_ip(ip_hex: str, binary_path: str, addr2line_exe: str = DEFAULT_ADDR2LINE) -> dict[str, Any]:
    """Translate a target instruction pointer to source function, file, and line number."""
    ip_norm = (ip_hex or "").strip()
    if not HEX_IP_RE.fullmatch(ip_norm):
        return {"resolved": False, "error": "Invalid instruction pointer format (expected 0x... hex)"}
    if int(ip_norm, 16) == 0:
        return {"resolved": False, "error": "Null instruction pointer"}
    if not Path(binary_path).is_file():
        return {"resolved": False, "error": f"Binary not found: {binary_path}"}

    exe_resolved = addr2line_exe if Path(addr2line_exe).is_file() else (shutil.which(addr2line_exe) or "")
    if not exe_resolved:
        return {"resolved": False, "error": f"addr2line not found: {addr2line_exe}"}

    proc = subprocess.run(
        [exe_resolved, "-e", binary_path, "-f", "-C", ip_norm],
        capture_output=True,
        text=True,
        timeout=5,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 or len(lines) < 2:
        return {"resolved": False, "error": proc.stderr.strip() or "addr2line returned no symbols"}

    function_name = lines[0]
    location_raw = lines[1]
    match = LINE_RE.match(location_raw)
    if not match or match.group(1) == "??":
        return {
            "resolved": False,
            "function": function_name,
            "raw": location_raw,
            "error": "Address outside application debug symbols (??:0)",
        }

    file_path = match.group(1)
    line_num = int(match.group(2))
    return {
        "resolved": True,
        "ip_hex": ip_hex,
        "function": function_name,
        "file": file_path,
        "line": line_num,
    }


def _read_snippet(file_path: Path, center_line: int, radius: int = 20) -> str:
    if not file_path.is_file():
        return ""
    all_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, center_line - radius)
    end = min(len(all_lines), center_line + radius)
    out = []
    for idx in range(start, end + 1):
        marker = ">>>" if idx == center_line else "   "
        out.append(f"{marker} {idx:4d} | {all_lines[idx - 1]}")
    return "\n".join(out)


def _find_call_blocks(target_dir: Path, call_token: str, radius: int = 10) -> list[dict[str, Any]]:
    """Scan C/C++ source files recursively for non-comment occurrences of call_token."""
    blocks: list[dict[str, Any]] = []
    for src in iter_source_files(target_dir):
        text = src.read_text(encoding="utf-8", errors="replace")
        for idx, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if call_token in line and not stripped.startswith(("*", "//", "/*")):
                blocks.append({
                    "file": str(src),
                    "line": idx,
                    "snippet": _read_snippet(src, idx, radius=radius),
                })
    return blocks


def _find_server_channel_blocks(target_dir: Path) -> list[dict[str, Any]]:
    """Scan C/C++ source files recursively for ChannelCreate(...) initialization."""
    blocks = _find_call_blocks(target_dir, "ChannelCreate(", radius=10)
    # Put files containing _NTO_CHF_FIXED_PRIORITY first so buggy server config is highlighted
    blocks.sort(key=lambda b: "_NTO_CHF_FIXED_PRIORITY" not in b["snippet"])
    return blocks


def _find_server_handler_blocks(target_dir: Path) -> list[dict[str, Any]]:
    """Scan C/C++ source files recursively for MsgReceive(...) server handler loops."""
    return _find_call_blocks(target_dir, "MsgReceive(", radius=12)


def extract_code_context(
    ip_hex: str,
    target_dir: str,
    binary_path: str,
    addr2line_exe: str = DEFAULT_ADDR2LINE,
) -> dict[str, Any]:
    """Resolve client IP and pair it with server ChannelCreate and MsgReceive context."""
    resolved = resolve_ip(ip_hex, binary_path, addr2line_exe)
    client_snippet = ""
    if resolved.get("resolved"):
        client_snippet = _read_snippet(Path(resolved["file"]), resolved["line"], radius=20)

    target_root = Path(target_dir)
    return {
        "client_resolution": resolved,
        "client_snippet": client_snippet,
        "server_channels": _find_server_channel_blocks(target_root),
        "server_handlers": _find_server_handler_blocks(target_root),
    }
