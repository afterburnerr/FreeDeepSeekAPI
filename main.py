"""FreeDeepSeekAPI — an OpenAI-compatible proxy in front of chat.deepseek.com.

Exposes the standard OpenAI chat-completions surface on top of DeepSeek's
web API (via the `dsk` module from deepseek4free), so any OpenAI SDK / curl
script can talk to `https://chat.deepseek.com` without dealing with its
proof-of-work challenge or AWS-WAF cookie dance.

Endpoints
---------
* ``POST /v1/chat/completions`` — OpenAI-style (blocking or SSE streaming).
* ``GET  /v1/models``            — two models: ``deepseek-chat`` and
  ``deepseek-reasoner``.
* ``POST /admin/refresh-cookies`` — re-run the cookie bypass (Chrome).
* ``GET  /admin/health``          — readiness probe.

Streaming format
----------------
As of late 2025 DeepSeek switched from an OpenAI-style
``choices[].delta.content`` stream to a JSON-Patch operation stream
(``{"p":"response/content","o":"APPEND","v":"..."}``). ``dsk/api.py`` handles
both; this module just wires the generator into OpenAI chunks.

Session pool
------------
Every DeepSeek completion normally requires a ``create_chat_session`` call
first. We keep one session per ``(token, thinking, search)`` triplet and
rotate it after ``DEEPSEEK_SESSION_REUSE_LIMIT`` calls so conversation
history doesn't accumulate indefinitely server-side.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from dsk.api import DeepSeekAPI

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Optional fallback token used if a request omits the `Authorization: Bearer`
# header. In a single-user deployment this is usually set; in a multi-tenant
# one each client sends its own token.
FALLBACK_AUTH_TOKEN = os.getenv("DEEPSEEK_AUTH_TOKEN", "").strip()

# Default value for DeepSeek's built-in web search when the client doesn't
# supply `search_enabled` explicitly.
SEARCH_DEFAULT = os.getenv("DEEPSEEK_SEARCH_DEFAULT", "false").strip().lower() in (
    "true", "1", "yes", "on",
)

# How many completions to serve out of one upstream chat session before
# rotating. 30 is a good default — small enough to keep context clean for
# classifier-style calls, large enough to amortise session-creation RTT.
SESSION_REUSE_LIMIT = int(os.getenv("DEEPSEEK_SESSION_REUSE_LIMIT", "30"))

# Admin endpoints default to sharing the fallback DeepSeek token as their
# bearer secret, so in the common case no extra config is needed. Override
# via ADMIN_SECRET if you want a separate key.
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip() or FALLBACK_AUTH_TOKEN

# Bound the DeepSeekAPI client cache so a buggy client sending many distinct
# tokens can't balloon memory.
_CLIENT_CACHE_LIMIT = int(os.getenv("DEEPSEEK_CLIENT_CACHE_LIMIT", "16"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("freedeepseekapi")

# Silence noisy libraries at INFO.
for noisy in ("uvicorn.access", "httpcore", "httpx", "fastapi"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Client + session caches
# ---------------------------------------------------------------------------

_client_cache: Dict[str, DeepSeekAPI] = {}
_client_lock = Lock()


@dataclass
class SessionSlot:
    """A DeepSeek chat session along with its reuse counter."""
    chat_id: str
    used: int = 0
    created_at: float = field(default_factory=time.time)


# Pool keyed by (token, thinking, search) so classifier traffic (thinking=False)
# doesn't share a session with reasoning traffic.
_session_pool: Dict[Tuple[str, bool, bool], SessionSlot] = {}
_session_lock = Lock()


def _get_client_for_token(token: str) -> DeepSeekAPI:
    with _client_lock:
        client = _client_cache.get(token)
        if client is not None:
            return client
        if len(_client_cache) >= _CLIENT_CACHE_LIMIT:
            # Simple FIFO eviction — adequate for a local proxy.
            evicted = next(iter(_client_cache))
            _client_cache.pop(evicted, None)
            log.info("Evicted DeepSeekAPI client (cache full)")
        client = DeepSeekAPI(token)
        _client_cache[token] = client
        log.debug("Built new DeepSeekAPI client (cache size=%d)", len(_client_cache))
        return client


def _acquire_session(
    client: DeepSeekAPI, token: str, thinking: bool, search: bool
) -> Tuple[str, int, int]:
    """Return (chat_id, used_after_this_call, limit)."""
    key = (token, bool(thinking), bool(search))
    with _session_lock:
        slot = _session_pool.get(key)
        if slot is None or slot.used >= SESSION_REUSE_LIMIT:
            chat_id = client.create_chat_session()
            slot = SessionSlot(chat_id=chat_id)
            _session_pool[key] = slot
            log.info(
                "Rotated DeepSeek session: chat_id=%s thinking=%s search=%s",
                chat_id, thinking, search,
            )
        slot.used += 1
        return slot.chat_id, slot.used, SESSION_REUSE_LIMIT


# ---------------------------------------------------------------------------
# Request schema + helpers
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "deepseek-chat"
    messages: List[Message]
    stream: bool = False
    temperature: Optional[float] = None
    # Non-OpenAI extension: enable DeepSeek's built-in web search.
    search_enabled: Optional[bool] = None


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        candidate = auth.split(" ", 1)[1].strip()
        # Ignore OpenAI SDK placeholders.
        if candidate and candidate.lower() not in {"unused", "none", "null", "sk-", "sk-xxxx"}:
            return candidate
    if FALLBACK_AUTH_TOKEN:
        return FALLBACK_AUTH_TOKEN
    raise HTTPException(
        status_code=401,
        detail=(
            "Missing DeepSeek auth token. Pass it as `Authorization: Bearer <token>` "
            "or set DEEPSEEK_AUTH_TOKEN on the server."
        ),
    )


def _build_prompt(messages: List[Message]) -> str:
    """Collapse OpenAI-style messages into one DeepSeek prompt string.

    DeepSeek's chat API has no first-class system role — its web UI simply
    prepends instructions to the user turn. We replicate that so system
    prompts sent by clients (classifiers, tool-use agents, etc.) are actually
    honoured by the model instead of silently dropped.
    """
    system_parts = [m.content.strip() for m in messages if m.role == "system" and m.content]
    user_msgs = [m for m in messages if m.role == "user"]
    last_user = (
        user_msgs[-1].content
        if user_msgs
        else (messages[-1].content if messages else "")
    )
    if system_parts:
        header = "\n\n".join(system_parts)
        return f"{header}\n\n---\n\n{last_user}"
    return last_user


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info(
        "FreeDeepSeekAPI starting up — session_reuse_limit=%d search_default=%s "
        "admin_secret=%s fallback_token=%s",
        SESSION_REUSE_LIMIT,
        SEARCH_DEFAULT,
        "set" if ADMIN_SECRET else "missing",
        "set" if FALLBACK_AUTH_TOKEN else "missing",
    )
    if not FALLBACK_AUTH_TOKEN:
        log.warning(
            "DEEPSEEK_AUTH_TOKEN is not set — clients must supply their token via "
            "Authorization: Bearer <token>."
        )
    try:
        yield
    finally:
        log.info(
            "Shutting down; cached clients=%d, pooled sessions=%d",
            len(_client_cache), len(_session_pool),
        )


app = FastAPI(
    title="FreeDeepSeekAPI",
    description="OpenAI-compatible local proxy for chat.deepseek.com",
    version="0.2.0",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    token = _extract_token(request)
    client = _get_client_for_token(token)

    prompt = _build_prompt(req.messages)
    thinking = "r1" in req.model or "reasoner" in req.model
    search = req.search_enabled if req.search_enabled is not None else SEARCH_DEFAULT

    chat_id, used, limit = _acquire_session(client, token, thinking, search)

    if req.stream:
        return StreamingResponse(
            _stream_response(client, chat_id, prompt, thinking, search, req.model),
            media_type="text/event-stream",
            headers={
                "x-deepseek-session": chat_id,
                "x-deepseek-session-use": f"{used}/{limit}",
            },
        )

    text = ""
    for chunk in client.chat_completion(
        chat_id, prompt, thinking_enabled=thinking, search_enabled=search,
    ):
        if chunk.get("type") == "text":
            text += chunk.get("content", "")

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "deepseek_session": {"chat_id": chat_id, "use": used, "limit": limit},
    }


async def _stream_response(
    client: DeepSeekAPI,
    chat_id: str,
    prompt: str,
    thinking: bool,
    search: bool,
    model: str,
) -> AsyncGenerator[str, None]:
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    for chunk in client.chat_completion(
        chat_id, prompt, thinking_enabled=thinking, search_enabled=search,
    ):
        if chunk.get("type") == "text" and chunk.get("content"):
            data = {
                "id": resp_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk["content"]},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(data)}\n\n"

    data = {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(data)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
            {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"},
        ],
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

_admin_refresh_lock = asyncio.Lock()
_REFRESH_SCRIPT = Path(__file__).parent / "refresh_cookies.sh"
_REFRESH_TIMEOUT_SEC = int(os.getenv("ADMIN_REFRESH_TIMEOUT", "180"))


def _check_admin(request: Request) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Admin endpoints disabled: set ADMIN_SECRET or DEEPSEEK_AUTH_TOKEN.",
        )
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    candidate = (
        auth.split(" ", 1)[1].strip() if auth.lower().startswith("bearer ") else ""
    )
    if candidate != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="admin auth failed")


@app.post("/admin/refresh-cookies")
async def refresh_cookies_endpoint(request: Request):
    """Run the bundled refresh_cookies.sh and clear session/client caches."""
    _check_admin(request)
    if not _REFRESH_SCRIPT.exists():
        raise HTTPException(
            status_code=500,
            detail=f"refresh_cookies.sh not found at {_REFRESH_SCRIPT}",
        )

    if _admin_refresh_lock.locked():
        raise HTTPException(status_code=409, detail="refresh already in progress")

    async with _admin_refresh_lock:
        log.info("Admin refresh requested — spawning refresh_cookies.sh")
        proc = await asyncio.create_subprocess_exec(
            str(_REFRESH_SCRIPT),
            cwd=str(_REFRESH_SCRIPT.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw_out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_REFRESH_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.error("Admin refresh timed out after %ds", _REFRESH_TIMEOUT_SEC)
            raise HTTPException(status_code=504, detail="refresh timed out")

        out = raw_out.decode("utf-8", "ignore")
        tail = "\n".join(out.splitlines()[-8:])[:1200]

        if proc.returncode != 0:
            log.warning("Admin refresh exited with %d: %s", proc.returncode, tail)
            return {"ok": False, "exit_code": proc.returncode, "log_tail": tail}

        # Drop caches so next completion re-reads cookies.json.
        with _client_lock:
            _client_cache.clear()
        with _session_lock:
            _session_pool.clear()
        log.info("Admin refresh succeeded; client/session caches cleared")
        return {"ok": True, "exit_code": 0, "log_tail": tail, "ts": int(time.time())}


@app.get("/admin/health")
async def admin_health(request: Request):
    _check_admin(request)
    cookies_path = Path(__file__).parent / "dsk" / "cookies.json"
    return {
        "ok": True,
        "cookies_present": cookies_path.exists(),
        "cookies_mtime": (
            int(cookies_path.stat().st_mtime) if cookies_path.exists() else 0
        ),
        "sessions": len(_session_pool),
        "clients": len(_client_cache),
        "session_reuse_limit": SESSION_REUSE_LIMIT,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())
