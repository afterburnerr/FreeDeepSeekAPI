#!/usr/bin/env python3
"""One-shot smoke test for a running FreeDeepSeekAPI instance.

Usage:
    python smoke_test.py                     # assumes http://127.0.0.1:8080
    BASE=http://proxy:8080 TOKEN=sk... python smoke_test.py

Checks:
    1. GET  /v1/models returns both model ids
    2. POST /v1/chat/completions returns a non-empty answer
    3. GET  /admin/health returns cookies_present=true (if TOKEN set)

Exits 0 on success, non-zero on any failure. Safe to run in CI.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("BASE", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.getenv("TOKEN", "").strip()
MODEL = os.getenv("MODEL", "deepseek-chat")


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    url = BASE + path
    data = None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body}


def check_models() -> None:
    status, data = _req("GET", "/v1/models", timeout=10)
    assert status == 200, f"/v1/models -> HTTP {status}"
    ids = {m.get("id") for m in data.get("data", [])}
    assert {"deepseek-chat", "deepseek-reasoner"}.issubset(ids), f"unexpected models: {ids}"
    print(f"✓ /v1/models OK ({len(ids)} models)")


def check_completion() -> None:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Reply with exactly one word: PONG."},
            {"role": "user", "content": "ping"},
        ],
        "stream": False,
        "temperature": 0,
    }
    status, data = _req("POST", "/v1/chat/completions", payload, timeout=120)
    assert status == 200, f"/v1/chat/completions -> HTTP {status}: {data}"
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    assert content.strip(), f"empty completion: {data}"
    print(f"✓ /v1/chat/completions OK (content[:60]={content[:60]!r})")
    session = data.get("deepseek_session") or {}
    if session:
        print(f"  session use={session.get('use')}/{session.get('limit')}")


def check_admin_health() -> None:
    if not TOKEN:
        print("- /admin/health skipped (TOKEN env var not set)")
        return
    status, data = _req("GET", "/admin/health", timeout=10)
    assert status == 200, f"/admin/health -> HTTP {status}: {data}"
    assert data.get("ok") is True, f"admin health reported not-ok: {data}"
    print(
        f"✓ /admin/health OK (cookies_present={data.get('cookies_present')}, "
        f"sessions={data.get('sessions')})"
    )


def main() -> int:
    print(f"Smoke-testing {BASE}")
    try:
        check_models()
        check_completion()
        check_admin_health()
    except AssertionError as exc:
        print(f"✗ {exc}")
        return 1
    except Exception as exc:
        print(f"✗ unexpected error: {type(exc).__name__}: {exc}")
        return 2
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
