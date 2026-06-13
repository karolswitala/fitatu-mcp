"""Per-user FitatuClient pooling for multi-tenant deployments.

Three ingress paths converge on the same per-Fitatu-user FitatuClient instance:

  1. fitatu_login(email, password) tool  -> register_session -> X-Fitatu-Session
  2. X-Fitatu-Session: <session_id>      -> resolve_session
  3. Authorization: Basic b64(email:pwd) -> resolve_basic_cached / get_or_login

The pool never persists credentials to disk; all state lives in process memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field

from .fitatu_client import FitatuAuthError, FitatuClient

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TTL_SECONDS = 30 * 24 * 3600  # 30 days; matches Fitatu refresh-token validity ceiling
DEFAULT_MAX_POOL_SIZE = 64


@dataclass
class _PoolEntry:
    client: FitatuClient
    last_used: float = field(default_factory=time.monotonic)


def _hash_basic(b64_value: str) -> str:
    return hashlib.sha256(b64_value.encode("ascii")).hexdigest()


def decode_basic_header(b64_value: str) -> tuple[str, str]:
    """Decode `base64(email:password)` payload from an Authorization: Basic header.

    Raises ValueError on malformed input.
    """
    try:
        decoded = base64.b64decode(b64_value, validate=True).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"auth_invalid: Basic header decode failed: {exc}") from exc
    email, sep, password = decoded.partition(":")
    if not sep or not email or not password:
        raise ValueError("auth_invalid: Basic header must be base64(email:password)")
    return email, password


class SessionPool:
    """In-memory pool of FitatuClient instances keyed by Fitatu numeric user_id.

    Two indirection maps converge on the same pool entry:
      - session_id (random 256-bit) -> user_id
      - sha256(basic_b64)           -> user_id

    All write paths take an asyncio.Lock to prevent duplicate-login races
    when multiple concurrent requests carry the same Basic header for a cold pool.
    """

    def __init__(
        self,
        *,
        base_url_read: str | None = None,
        base_url_write: str | None = None,
        idle_ttl_seconds: int = DEFAULT_IDLE_TTL_SECONDS,
        max_size: int = DEFAULT_MAX_POOL_SIZE,
    ):
        self._by_user: dict[str, _PoolEntry] = {}
        self._sessions: dict[str, str] = {}
        self._basic: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._base_url_read = base_url_read
        self._base_url_write = base_url_write
        self._idle_ttl = idle_ttl_seconds
        self._max_size = max_size
        # Test seam: when no inbound request context is available (in-process
        # mcp.call_tool() during unit tests), _resolve_client falls back to
        # this client. Production never sets this.
        self._test_default_client: FitatuClient | None = None

    # ---- write paths (locked) ----

    async def get_or_login(self, email: str, password: str) -> FitatuClient:
        """Return cached client for these creds, or log in to Fitatu and cache."""
        b64 = base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")
        bhash = _hash_basic(b64)

        async with self._lock:
            self._evict_idle_locked()

            # Fast path: Basic header already seen.
            user_id = self._basic.get(bhash)
            if user_id:
                entry = self._by_user.get(user_id)
                if entry is not None:
                    entry.last_used = time.monotonic()
                    return entry.client

            # Cold path: log in. login() is sync (requests), offload to thread.
            client = FitatuClient(
                email,
                password,
                base_url_read=self._base_url_read,
                base_url_write=self._base_url_write,
            )
            await asyncio.to_thread(client.login)
            if not client.user_id:
                raise FitatuAuthError("login returned no user_id")

            # Convergence: if another path (session) already registered this user, reuse it.
            existing = self._by_user.get(client.user_id)
            if existing is not None:
                existing.last_used = time.monotonic()
                self._basic[bhash] = client.user_id
                return existing.client

            self._by_user[client.user_id] = _PoolEntry(client=client)
            self._basic[bhash] = client.user_id
            self._enforce_size_locked()
            return client

    async def register_session(self, client: FitatuClient) -> str:
        """Mint a fresh session_id for an already-logged-in client and cache it."""
        if not client.user_id:
            raise FitatuAuthError("client has no user_id; call login first")
        sid = secrets.token_urlsafe(32)
        async with self._lock:
            self._sessions[sid] = client.user_id
            entry = self._by_user.get(client.user_id)
            if entry is None:
                self._by_user[client.user_id] = _PoolEntry(client=client)
            else:
                entry.last_used = time.monotonic()
        return sid

    # ---- read paths (sync, lock-free; dict reads are atomic under GIL) ----

    def resolve_session(self, session_id: str) -> FitatuClient | None:
        uid = self._sessions.get(session_id)
        if not uid:
            return None
        entry = self._by_user.get(uid)
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        return entry.client

    def resolve_basic_cached(self, b64_value: str) -> FitatuClient | None:
        bhash = _hash_basic(b64_value)
        uid = self._basic.get(bhash)
        if not uid:
            return None
        entry = self._by_user.get(uid)
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        return entry.client

    # ---- maintenance ----

    def _evict_idle_locked(self) -> None:
        now = time.monotonic()
        stale = [uid for uid, e in self._by_user.items() if now - e.last_used > self._idle_ttl]
        for uid in stale:
            self._drop_user_locked(uid)

    def _enforce_size_locked(self) -> None:
        if len(self._by_user) <= self._max_size:
            return
        ordered = sorted(self._by_user.items(), key=lambda kv: kv[1].last_used)
        overflow = len(self._by_user) - self._max_size
        for uid, _ in ordered[:overflow]:
            self._drop_user_locked(uid)

    def _drop_user_locked(self, uid: str) -> None:
        self._by_user.pop(uid, None)
        for sid, suid in list(self._sessions.items()):
            if suid == uid:
                self._sessions.pop(sid)
        for bh, buid in list(self._basic.items()):
            if buid == uid:
                self._basic.pop(bh)

    # ---- introspection (tests) ----

    def __len__(self) -> int:
        return len(self._by_user)

    def stats(self) -> dict:
        return {
            "users": len(self._by_user),
            "sessions": len(self._sessions),
            "basic_cached": len(self._basic),
        }
