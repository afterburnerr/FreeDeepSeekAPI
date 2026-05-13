"""Thin wrapper around the DeepSeek web chat API.

The public surface is ``DeepSeekAPI``:

    api = DeepSeekAPI(auth_token)
    chat_id = api.create_chat_session()
    for chunk in api.chat_completion(chat_id, "Hello"):
        print(chunk)  # {"content": "...", "type": "text", "finish_reason": ...}

Streaming format
----------------
Historically DeepSeek used an OpenAI-style ``choices[].delta.content`` SSE.
In late 2025 it switched to a JSON-Patch-like stream over the same
``data: ...`` frames — operations like ``APPEND`` and ``SET`` on a response
tree. ``_iter_parsed`` supports both formats and emits a unified shape:

    {"content": str, "type": "text" | "thinking", "finish_reason": str | None}

The flag ``finish_reason == "stop"`` is the caller's signal to break out.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Dict, Generator, Literal, Optional

from curl_cffi import requests

from .pow import DeepSeekPOW

log = logging.getLogger(__name__)

ThinkingMode = Literal["detailed", "simple", "disabled"]
SearchMode = Literal["enabled", "disabled"]

_REQUIRED_CURL_CFFI = "0.8.1b9"
_BROWSER_IMPERSONATION = "chrome120"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DeepSeekError(Exception):
    """Base exception for DeepSeek API failures."""


class AuthenticationError(DeepSeekError):
    """Raised when the DeepSeek auth token is missing / rejected."""


class RateLimitError(DeepSeekError):
    """Raised when DeepSeek returns 429."""


class NetworkError(DeepSeekError):
    """Raised on low-level network / transport failures."""


class CloudflareError(DeepSeekError):
    """Raised when a CloudFlare / AWS WAF challenge wall blocks us."""


class APIError(DeepSeekError):
    """Raised on any other non-2xx or malformed response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _warn_curl_cffi_version() -> None:
    try:
        installed = pkg_version("curl-cffi")
    except PackageNotFoundError:
        log.warning(
            "curl-cffi is not installed. Install the pinned version: "
            "pip install curl-cffi==%s", _REQUIRED_CURL_CFFI,
        )
        return
    if installed != _REQUIRED_CURL_CFFI:
        log.warning(
            "curl-cffi version mismatch (installed=%s, expected=%s). "
            "Different versions may fail to bypass DeepSeek's browser "
            "impersonation checks.",
            installed, _REQUIRED_CURL_CFFI,
        )


def _load_cookies_from(path: Path) -> Dict[str, str]:
    if not path.exists():
        log.warning("Cookies file not found at %s; requests may fail WAF", path)
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load cookies from %s: %s", path, exc)
        return {}
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    return cookies if isinstance(cookies, dict) else {}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DeepSeekAPI:
    """Client for ``https://chat.deepseek.com/api/v0``.

    The class is intentionally stateful: cookies and the POW solver are
    held on the instance. Thread-safety: not guaranteed — wrap in a lock
    if you share one client across threads.
    """

    BASE_URL = "https://chat.deepseek.com/api/v0"

    def __init__(self, auth_token: str):
        if not auth_token or not isinstance(auth_token, str):
            raise AuthenticationError("Invalid auth token provided")

        _warn_curl_cffi_version()

        self.auth_token = auth_token
        self.pow_solver = DeepSeekPOW()
        self._cookies_path = Path(__file__).parent / "cookies.json"
        self.cookies: Dict[str, str] = _load_cookies_from(self._cookies_path)

        # State used by _iter_parsed to track path for bare {"v": ...}
        # continuation chunks. Reset at the start of every chat_completion.
        self._last_stream_path: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Low-level request plumbing
    # ------------------------------------------------------------------ #

    def _get_headers(self, pow_response: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": (
                "en,fr-FR;q=0.9,fr;q=0.8,es-ES;q=0.7,es;q=0.6,"
                "en-US;q=0.5,am;q=0.4,de;q=0.3"
            ),
            "authorization": f"Bearer {self.auth_token}",
            "content-type": "application/json",
            "origin": "https://chat.deepseek.com",
            "referer": "https://chat.deepseek.com/",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "x-app-version": "20241129.1",
            "x-client-locale": "en_US",
            "x-client-platform": "web",
            "x-client-version": "1.0.0-always",
        }
        if pow_response:
            headers["x-ds-pow-response"] = pow_response
        return headers

    def _refresh_cookies(self) -> None:
        """Invoke the external bypass helper to rotate cookies.json."""
        try:
            script = Path(__file__).parent / "bypass.py"
            subprocess.run([sys.executable, str(script)], check=True)
            time.sleep(2)
            self.cookies = _load_cookies_from(self._cookies_path)
            log.info("Cookies refreshed (%d keys)", len(self.cookies))
        except Exception as exc:
            log.warning("Failed to refresh cookies: %s", exc)

    def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: Dict[str, Any],
        pow_required: bool = False,
    ) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        max_retries = 2

        for attempt in range(max_retries):
            headers = self._get_headers()
            if pow_required:
                challenge = self._get_pow_challenge()
                headers = self._get_headers(
                    pow_response=self.pow_solver.solve_challenge(challenge)
                )

            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    cookies=self.cookies,
                    impersonate=_BROWSER_IMPERSONATION,
                    timeout=None,
                )
            except requests.exceptions.RequestException as exc:
                raise NetworkError(f"Network error: {exc}") from exc

            # Check for edge challenge first (returned as HTML with 200).
            if "<!DOCTYPE html>" in response.text and (
                "Just a moment" in response.text or "challenge" in response.text.lower()
            ):
                log.warning(
                    "Edge challenge detected (attempt %d/%d), refreshing cookies",
                    attempt + 1, max_retries,
                )
                if attempt < max_retries - 1:
                    self._refresh_cookies()
                    continue

            if response.status_code == 401:
                raise AuthenticationError("Invalid or expired authentication token")
            if response.status_code == 429:
                raise RateLimitError("API rate limit exceeded")
            if response.status_code >= 500:
                raise APIError(f"Server error: {response.text}", response.status_code)
            if response.status_code != 200:
                raise APIError(
                    f"API request failed: {response.text}", response.status_code
                )

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise APIError("Invalid JSON response from server") from exc

        raise CloudflareError(
            "Failed to bypass edge protection after multiple attempts"
        )

    # ------------------------------------------------------------------ #
    # High-level API
    # ------------------------------------------------------------------ #

    def _get_pow_challenge(self) -> Dict[str, Any]:
        try:
            resp = self._make_request(
                "POST",
                "/chat/create_pow_challenge",
                {"target_path": "/api/v0/chat/completion"},
            )
            return resp["data"]["biz_data"]["challenge"]
        except KeyError as exc:
            raise APIError("Invalid POW challenge response format") from exc

    def create_chat_session(self) -> str:
        """Create a new chat session and return its id."""
        try:
            resp = self._make_request(
                "POST",
                "/chat_session/create",
                {"character_id": None},
            )
            return resp["data"]["biz_data"]["id"]
        except KeyError as exc:
            raise APIError("Invalid session creation response format") from exc

    def chat_completion(
        self,
        chat_session_id: str,
        prompt: str,
        parent_message_id: Optional[str] = None,
        thinking_enabled: bool = True,
        search_enabled: bool = False,
    ) -> Generator[Dict[str, Any], None, None]:
        """Send a prompt and yield response chunks.

        Yields dicts with keys:

        * ``content`` — partial text (may be empty on the terminating chunk)
        * ``type`` — either ``"text"`` or ``"thinking"``
        * ``finish_reason`` — ``None`` while streaming, ``"stop"`` at the end
        """
        if not prompt or not isinstance(prompt, str):
            raise ValueError("Prompt must be a non-empty string")
        if not chat_session_id or not isinstance(chat_session_id, str):
            raise ValueError("Chat session ID must be a non-empty string")

        payload = {
            "chat_session_id": chat_session_id,
            "parent_message_id": parent_message_id,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
        }

        self._last_stream_path = None

        headers = self._get_headers(
            pow_response=self.pow_solver.solve_challenge(self._get_pow_challenge())
        )

        try:
            response = requests.post(
                f"{self.BASE_URL}/chat/completion",
                headers=headers,
                json=payload,
                cookies=self.cookies,
                impersonate=_BROWSER_IMPERSONATION,
                stream=True,
                timeout=None,
            )
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"Network error during streaming: {exc}") from exc

        if response.status_code != 200:
            error_text = next(response.iter_lines(), b"").decode("utf-8", "ignore")
            if response.status_code == 401:
                raise AuthenticationError("Invalid or expired authentication token")
            if response.status_code == 429:
                raise RateLimitError("API rate limit exceeded")
            raise APIError(
                f"API request failed: {error_text}", response.status_code
            )

        for line in response.iter_lines():
            for parsed in self._iter_parsed(line):
                yield parsed
                if parsed.get("finish_reason") == "stop":
                    return

    # ------------------------------------------------------------------ #
    # Stream parser
    # ------------------------------------------------------------------ #

    def _iter_parsed(self, chunk: bytes):
        """Yield zero or more normalised chunks from a raw SSE line.

        Handles three formats in order of precedence:

        1. **Legacy** — ``data: {"choices":[{"delta":{"content":"..."}}]}``
        2. **Snapshot** — ``data: {"v": {"response": {...initial object...}}}``
           (on session reuse DeepSeek may pre-fill content here and stream
           only the delta afterwards — so we must surface it).
        3. **JSON-Patch** — ``data: {"p":"response/content","o":"APPEND","v":"tok"}``
           and bare continuations like ``{"v":"more"}`` that implicitly
           target the previous path.
        """
        if not chunk:
            return
        if chunk.startswith(b"event:"):
            return
        if not chunk.startswith(b"data: "):
            return
        try:
            data = json.loads(chunk[6:])
        except json.JSONDecodeError:
            return

        if not isinstance(data, dict):
            return

        # (1) Legacy OpenAI-style delta — keep supported for old clients.
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                yield {
                    "content": delta.get("content", "") or "",
                    "type": delta.get("type") or "text",
                    "finish_reason": choice.get("finish_reason"),
                }
                return

        # (2) Snapshot: {"v": {...response object...}}
        if "p" not in data and "o" not in data and isinstance(data.get("v"), dict):
            yield from self._from_snapshot(data["v"])
            return

        # (3) JSON-Patch operations
        yield from self._from_patch(data)

    def _from_snapshot(self, snapshot: Dict[str, Any]):
        resp = snapshot.get("response") if isinstance(snapshot, dict) else None
        if not isinstance(resp, dict):
            return
        pre_text = resp.get("content")
        if isinstance(pre_text, str) and pre_text:
            self._last_stream_path = "response/content"
            yield {"content": pre_text, "type": "text", "finish_reason": None}
        pre_think = resp.get("thinking_content")
        if isinstance(pre_think, str) and pre_think:
            self._last_stream_path = "response/thinking_content"
            yield {"content": pre_think, "type": "thinking", "finish_reason": None}

    def _from_patch(self, data: Dict[str, Any]):
        path = data.get("p")
        op = data.get("o")
        value = data.get("v")
        last_path = self._last_stream_path

        # Explicit APPEND to a known content path.
        if path == "response/content" and (op == "APPEND" or op is None):
            self._last_stream_path = "response/content"
            yield {
                "content": str(value or ""),
                "type": "text",
                "finish_reason": None,
            }
            return

        if path == "response/thinking_content" and (op == "APPEND" or op is None):
            self._last_stream_path = "response/thinking_content"
            yield {
                "content": str(value or ""),
                "type": "thinking",
                "finish_reason": None,
            }
            return

        # Bare {"v": "..."} continuation: no path or op, implicitly targets
        # whichever path we last APPENDed to.
        if (
            path is None
            and op is None
            and isinstance(value, str)
            and last_path in ("response/content", "response/thinking_content")
        ):
            ctype = (
                "thinking"
                if last_path == "response/thinking_content"
                else "text"
            )
            yield {"content": value, "type": ctype, "finish_reason": None}
            return

        # Terminal status.
        if path == "response/status" and value == "FINISHED":
            self._last_stream_path = None
            yield {"content": "", "type": "text", "finish_reason": "stop"}
