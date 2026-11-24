"""Hybrid AI Copilot engine: Gemini API, optional local LM Studio, and safe QNX fallback."""

import asyncio
import json
import os
import re
from typing import Any

import httpx

SYSTEM_INSTRUCTION = (
    "You are a senior QNX Neutrino RTOS 8.0 systems engineer. "
    "Analyze the runtime IPC/thread snapshot and the resolved C/C++ source snippets (client call site and server ChannelCreate setup). "
    "Distinguish active C/C++ statements from comments, and pay close attention to measured latency vs the 10,000 us real-time budget. "
    "If active ChannelCreate(0) is already used and measured latency is <= 10,000 us with no blocking anomaly, respond with severity \"HEALTHY\", "
    "explain that Priority Inheritance is active and latency is healthy, and set code_before/code_after to \"// No change needed\". "
    "Respond ONLY with a valid JSON object containing exact keys: "
    "\"root_cause\" (short title), "
    "\"severity\" (CRITICAL | WARNING | HEALTHY), "
    "\"qnx_explanation\" (clear technical explanation referencing QNX microkernel behavior), "
    "\"file_location\" (exact file and line), "
    "\"code_before\" (the exact buggy C/C++ line or \"// No change needed\"), "
    "\"code_after\" (the exact fixed C/C++ line or \"// ChannelCreate(0) priority inheritance already enabled\")."
)


def _strip_json_fences(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    return json.loads(cleaned)


def _build_prompt(anomaly: dict[str, Any], context: dict[str, Any]) -> str:
    res = context.get("client_resolution", {})
    server_text = "\n\n".join(
        f"Server Channel File: {b['file']} (line {b['line']})\n```c\n{b['snippet']}\n```"
        for b in context.get("server_channels", [])[:2]
    )
    return (
        f"### Runtime Anomaly Snapshot\n"
        f"- PID: {anomaly.get('pid')}, TID: {anomaly.get('tid')}, Priority: {anomaly.get('priority')}\n"
        f"- Thread State: {anomaly.get('state_name')} (blocked_streak={anomaly.get('blocked_streak', 0)})\n"
        f"- Measured IPC Latency: {anomaly.get('latency_us')} us\n"
        f"- Trigger Reason: {anomaly.get('reason')}\n\n"
        f"### Client Call Site (resolved via ntox86_64-addr2line)\n"
        f"- Function: {res.get('function', 'unknown')}\n"
        f"- Location: {res.get('file', 'unknown')}:{res.get('line', 0)}\n"
        f"```c\n{context.get('client_snippet', '(no snippet)')}\n```\n\n"
        f"### Server Channel Initialization Context\n"
        f"{server_text or '(no ChannelCreate found)'}\n"
    )


def _has_active_fixed_priority_flag(snippet: str) -> bool:
    for ln in snippet.splitlines():
        code_part = ln.split("|", 1)[-1].strip()
        if code_part.startswith(("*", "//", "/*")):
            continue
        if "_NTO_CHF_FIXED_PRIORITY" in code_part:
            return True
    return False


def _rule_based_fallback(anomaly: dict[str, Any], context: dict[str, Any], note: str) -> dict[str, Any]:
    """Deterministic QNX diagnostic fallback if no API key is configured or offline."""
    res = context.get("client_resolution", {})
    client_loc = f"{res.get('file', 'unknown')}:{res.get('line', 0)}"
    server_channels = context.get("server_channels", [])
    buggy_server = next((b for b in server_channels if _has_active_fixed_priority_flag(b["snippet"])), None)
    latency_us = int(anomaly.get("latency_us") or 0)

    if buggy_server:
        return {
            "provider": f"fallback ({note})",
            "root_cause": "QNX IPC Priority Inversion via _NTO_CHF_FIXED_PRIORITY",
            "severity": "CRITICAL",
            "qnx_explanation": (
                f"Thread prioritas tinggi (Prio {anomaly.get('priority')}) terhambat {latency_us} us "
                f"di {client_loc}. Channel server dibuat dengan flag _NTO_CHF_FIXED_PRIORITY yang mematikan "
                f"Priority Inheritance bawaan kernel QNX Neutrino, sehingga server (Prio 10) disela oleh thread menengah."
            ),
            "file_location": f"{buggy_server['file']}:{buggy_server['line']} (blocked at {client_loc})",
            "code_before": "int chid = ChannelCreate(_NTO_CHF_FIXED_PRIORITY);",
            "code_after": "int chid = ChannelCreate(0); // Enable QNX kernel priority inheritance",
        }

    if latency_us <= 10_000 and anomaly.get("state_name") != "STATE_REPLY":
        srv_loc = f"{server_channels[0]['file']}:{server_channels[0]['line']}" if server_channels else client_loc
        return {
            "provider": f"fallback ({note})",
            "root_cause": "Healthy Real-Time QNX IPC (Priority Inheritance Active)",
            "severity": "HEALTHY",
            "qnx_explanation": (
                f"Latensi IPC terukur {latency_us} us berada di bawah batas real-time 10,000 us. "
                f"Server menggunakan ChannelCreate(0) sehingga Priority Inheritance kernel QNX aktif dan mencegah preempt berkepanjangan."
            ),
            "file_location": srv_loc,
            "code_before": "// No change needed (ChannelCreate(0) active)",
            "code_after": "// Real-time budget < 10,000 us verified healthy",
        }

    return {
        "provider": f"fallback ({note})",
        "root_cause": f"IPC Latency Spike / Thread Blocking ({anomaly.get('state_name')})",
        "severity": "WARNING",
        "qnx_explanation": (
            f"Terdeteksi lonjakan latensi {anomaly.get('latency_us')} us pada TID {anomaly.get('tid')} "
            f"(Prio {anomaly.get('priority')}) di {client_loc}. Periksa beban CPU thread penerima dan prioritas mutex/channel."
        ),
        "file_location": client_loc,
        "code_before": f"// Blocked around line {res.get('line', 0)} in {res.get('function', 'client')}",
        "code_after": "// Verify ChannelCreate(0) priority inheritance and reduce critical section work",
    }


async def _call_gemini(prompt: str, api_key: str, model: str) -> dict[str, Any]:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    candidates = [model] + ([m for m in ("gemini-3.5-flash-lite",) if m != model])
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=12.0) as client:
        for active_model in candidates:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent"
            try:
                resp = await client.post(url, params={"key": api_key}, json=payload)
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = _strip_json_fences(text)
                parsed["provider"] = f"gemini ({active_model})"
                return parsed
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPStatusError) as exc:
                last_exc = exc
                await asyncio.sleep(0.3)
    assert last_exc is not None
    raise last_exc


async def _call_lmstudio(prompt: str, url: str, model: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = data["choices"][0]["message"]["content"]
    parsed = _strip_json_fences(text)
    parsed["provider"] = f"lmstudio ({model})"
    return parsed


async def diagnose_anomaly(anomaly: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run hybrid AI diagnosis using Gemini API, LM Studio, or deterministic fallback."""
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip()
    lm_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1/chat/completions").strip()
    lm_model = os.getenv("LMSTUDIO_MODEL", "local-model").strip()

    prompt = _build_prompt(anomaly, context)

    if provider == "gemini":
        if not api_key or api_key == "your_gemini_api_key_here":
            return _rule_based_fallback(anomaly, context, "GEMINI_API_KEY not set")
        try:
            return await _call_gemini(prompt, api_key, gemini_model)
        except httpx.HTTPStatusError as exc:
            return _rule_based_fallback(anomaly, context, f"gemini HTTP {exc.response.status_code}")
        except Exception as exc:
            return _rule_based_fallback(anomaly, context, f"gemini error: {exc.__class__.__name__}")

    if provider == "lmstudio":
        try:
            return await _call_lmstudio(prompt, lm_url, lm_model)
        except Exception as exc:
            return _rule_based_fallback(anomaly, context, f"lmstudio error: {exc.__class__.__name__}")

    return _rule_based_fallback(anomaly, context, "rule-engine mode")
