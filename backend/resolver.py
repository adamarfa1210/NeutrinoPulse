"""Symbol resolution via ntox86_64-addr2line and QNX client/server code context pairing."""

from pathlib import Path
import re
import subprocess
from typing import Any

DEFAULT_ADDR2LINE = r"C:\qnx800\host\win64\x86_64\usr\bin\ntox86_64-addr2line.exe"
LINE_RE = re.compile(r"^(.*):(\d+)(?:\s+\(discriminator.*\))?$")


def resolve_ip(ip_hex: str, binary_path: str, addr2line_exe: str = DEFAULT_ADDR2LINE) -> dict[str, Any]:
    """Translate a target instruction pointer to source function, file, and line number."""
    if not ip_hex or ip_hex == "0x0":
        return {"resolved": False, "error": "Null instruction pointer"}
    if not Path(binary_path).is_file():
        return {"resolved": False, "error": f"Binary not found: {binary_path}"}
    if not Path(addr2line_exe).is_file():
        return {"resolved": False, "error": f"addr2line not found: {addr2line_exe}"}

    proc = subprocess.run(
        [addr2line_exe, "-e", binary_path, "-f", "-C", ip_hex],
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


def _find_server_channel_blocks(target_dir: Path) -> list[dict[str, Any]]:
    """Scan C source files for ChannelCreate(...) initialization so LLM sees server flags."""
    blocks: list[dict[str, Any]] = []
    if not target_dir.is_dir():
        return blocks

    for src in sorted(target_dir.glob("*.c")):
        text = src.read_text(encoding="utf-8", errors="replace")
        for idx, line in enumerate(text.splitlines(), start=1):
            if "ChannelCreate(" in line:
                blocks.append({
                    "file": str(src),
                    "line": idx,
                    "snippet": _read_snippet(src, idx, radius=10),
                })
    # Put files containing _NTO_CHF_FIXED_PRIORITY first so buggy server config is highlighted
    blocks.sort(key=lambda b: "_NTO_CHF_FIXED_PRIORITY" not in b["snippet"])
    return blocks


def extract_code_context(
    ip_hex: str,
    target_dir: str,
    binary_path: str,
    addr2line_exe: str = DEFAULT_ADDR2LINE,
) -> dict[str, Any]:
    """Resolve client IP and pair it with server ChannelCreate context."""
    resolved = resolve_ip(ip_hex, binary_path, addr2line_exe)
    client_snippet = ""
    if resolved.get("resolved"):
        client_snippet = _read_snippet(Path(resolved["file"]), resolved["line"], radius=20)

    server_blocks = _find_server_channel_blocks(Path(target_dir))
    return {
        "client_resolution": resolved,
        "client_snippet": client_snippet,
        "server_channels": server_blocks,
    }
