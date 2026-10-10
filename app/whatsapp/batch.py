"""Debounced WhatsApp burst batching: pending registry, lock, flush delay.

Single worker today. Several workers can ingest the same conversation out of
order (RQ does not preserve webhook arrival across concurrent jobs). A cheap
Redis INCR seq orders the pending list by ingest-start, not WhatsApp time —
do not treat that as multi-worker support.

Vision `product_description` is not stored on InboundImage, so a recognised
photo's developer_item is kept on the pending Redis entry (TTL) rather than
rebuilt from the DB.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import settings
from app.whatsapp.service import truncate_wamid

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_PRODUCT_PHOTO = "product_photo"

REGISTRY_KEY_PREFIX = "whatsapp:batch:"
LOCK_KEY_PREFIX = "whatsapp:batch:lock:"
SEQ_KEY_PREFIX = "whatsapp:batch:seq:"
DEFAULT_REGISTRY_TTL_SECONDS = 300
DEFAULT_LOCK_TTL_SECONDS = 180

_LUA_APPEND = """
local raw = redis.call('GET', KEYS[1])
local data
if raw then
  data = cjson.decode(raw)
else
  data = {first_arrival = tonumber(ARGV[2]), token = ARGV[3],
          phone_number_id = ARGV[4], entries = {}}
end
local entry = cjson.decode(ARGV[1])
table.insert(data.entries, entry)
data.token = ARGV[3]
if data.phone_number_id == nil or data.phone_number_id == '' then
  data.phone_number_id = ARGV[4]
end
if data.first_arrival == nil then
  data.first_arrival = tonumber(ARGV[2])
end
redis.call('SET', KEYS[1], cjson.encode(data), 'EX', tonumber(ARGV[5]))
return redis.call('GET', KEYS[1])
"""

_LUA_TAKE = """
local raw = redis.call('GET', KEYS[1])
if (not raw) then
  return nil
end
redis.call('DEL', KEYS[1])
return raw
"""


def now_seconds() -> float:
    return time.time()


def registry_key(merchant_id: uuid.UUID, customer_phone: str) -> str:
    return f"{REGISTRY_KEY_PREFIX}{merchant_id}:{customer_phone}"


def lock_key(merchant_id: uuid.UUID, customer_phone: str) -> str:
    return f"{LOCK_KEY_PREFIX}{merchant_id}:{customer_phone}"


def seq_key(merchant_id: uuid.UUID, customer_phone: str) -> str:
    return f"{SEQ_KEY_PREFIX}{merchant_id}:{customer_phone}"


def flush_delay_seconds(
    *,
    quiet: float,
    max_wait: float,
    first_arrival: float,
    now: float,
) -> float:
    """Seconds until the next flush. quiet <= 0 disables batching."""
    if quiet <= 0:
        return 0.0
    remaining_max = max(0.0, first_arrival + max_wait - now)
    return min(quiet, remaining_max)


def batch_settings() -> tuple[float, float]:
    return (
        float(settings.whatsapp_batch_quiet_seconds),
        float(settings.whatsapp_batch_max_wait_seconds),
    )


@dataclass
class PendingEntry:
    kind: str
    message_row_id: str
    wamid: str | None
    reply_to_wamid: str | None = None
    developer_item: dict[str, Any] | None = None
    seq: int = 0

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "message_row_id": self.message_row_id,
            "wamid": self.wamid,
            "reply_to_wamid": self.reply_to_wamid,
            "seq": self.seq,
        }
        if self.developer_item is not None:
            payload["developer_item"] = self.developer_item
        return payload

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PendingEntry:
        return cls(
            kind=str(raw.get("kind") or KIND_TEXT),
            message_row_id=str(raw.get("message_row_id") or ""),
            wamid=raw.get("wamid"),
            reply_to_wamid=raw.get("reply_to_wamid"),
            developer_item=raw.get("developer_item"),
            seq=int(raw.get("seq") or 0),
        )


@dataclass
class BatchState:
    first_arrival: float
    token: str
    phone_number_id: str
    entries: list[PendingEntry] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "first_arrival": self.first_arrival,
            "token": self.token,
            "phone_number_id": self.phone_number_id,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> BatchState:
        entries = [
            PendingEntry.from_json(item)
            for item in (raw.get("entries") or [])
            if isinstance(item, dict)
        ]
        entries.sort(key=lambda item: item.seq)
        return cls(
            first_arrival=float(raw.get("first_arrival") or 0.0),
            token=str(raw.get("token") or ""),
            phone_number_id=str(raw.get("phone_number_id") or ""),
            entries=entries,
        )


class BatchBackend(Protocol):
    def next_seq(self, merchant_id: uuid.UUID, customer_phone: str) -> int: ...

    def append(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        entry: PendingEntry,
        *,
        now: float,
        token: str,
        phone_number_id: str,
        ttl: int = DEFAULT_REGISTRY_TTL_SECONDS,
    ) -> BatchState: ...

    def peek(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None: ...

    def take(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None: ...

    def clear(self, merchant_id: uuid.UUID, customer_phone: str) -> None: ...

    def acquire_lock(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        *,
        ttl: int = DEFAULT_LOCK_TTL_SECONDS,
    ) -> bool: ...

    def release_lock(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> None: ...


class MemoryBatchBackend:
    """In-memory backend for tests. Same methods as Redis; never sleeps."""

    def __init__(self) -> None:
        self._states: dict[str, BatchState] = {}
        self._seqs: dict[str, int] = {}
        self._locks: dict[str, str] = {}
        self._guard = threading.Lock()

    def next_seq(self, merchant_id: uuid.UUID, customer_phone: str) -> int:
        key = seq_key(merchant_id, customer_phone)
        with self._guard:
            self._seqs[key] = self._seqs.get(key, 0) + 1
            return self._seqs[key]

    def append(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        entry: PendingEntry,
        *,
        now: float,
        token: str,
        phone_number_id: str,
        ttl: int = DEFAULT_REGISTRY_TTL_SECONDS,
    ) -> BatchState:
        key = registry_key(merchant_id, customer_phone)
        with self._guard:
            state = self._states.get(key)
            if state is None:
                state = BatchState(
                    first_arrival=now,
                    token=token,
                    phone_number_id=phone_number_id,
                    entries=[],
                )
            if not state.phone_number_id:
                state.phone_number_id = phone_number_id
            state.token = token
            state.entries.append(entry)
            self._states[key] = state
            return BatchState(
                first_arrival=state.first_arrival,
                token=state.token,
                phone_number_id=state.phone_number_id,
                entries=list(state.entries),
            )

    def peek(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None:
        key = registry_key(merchant_id, customer_phone)
        with self._guard:
            state = self._states.get(key)
            if state is None:
                return None
            return BatchState(
                first_arrival=state.first_arrival,
                token=state.token,
                phone_number_id=state.phone_number_id,
                entries=list(state.entries),
            )

    def take(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None:
        key = registry_key(merchant_id, customer_phone)
        with self._guard:
            return self._states.pop(key, None)

    def clear(self, merchant_id: uuid.UUID, customer_phone: str) -> None:
        key = registry_key(merchant_id, customer_phone)
        with self._guard:
            self._states.pop(key, None)

    def acquire_lock(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        *,
        ttl: int = DEFAULT_LOCK_TTL_SECONDS,
    ) -> bool:
        key = lock_key(merchant_id, customer_phone)
        with self._guard:
            if key in self._locks:
                return False
            self._locks[key] = "1"
            return True

    def release_lock(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> None:
        key = lock_key(merchant_id, customer_phone)
        with self._guard:
            self._locks.pop(key, None)


class RedisBatchBackend:
    def __init__(self, redis=None) -> None:
        self._redis = redis

    def _conn(self):
        if self._redis is not None:
            return self._redis
        from redis import Redis

        return Redis.from_url(settings.redis_url)

    def next_seq(self, merchant_id: uuid.UUID, customer_phone: str) -> int:
        return int(self._conn().incr(seq_key(merchant_id, customer_phone)))

    def append(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        entry: PendingEntry,
        *,
        now: float,
        token: str,
        phone_number_id: str,
        ttl: int = DEFAULT_REGISTRY_TTL_SECONDS,
    ) -> BatchState:
        raw = self._conn().eval(
            _LUA_APPEND,
            1,
            registry_key(merchant_id, customer_phone),
            json.dumps(entry.to_json(), ensure_ascii=False),
            str(now),
            token,
            phone_number_id,
            str(ttl),
        )
        payload = json.loads(raw)
        return BatchState.from_json(payload)

    def peek(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None:
        raw = self._conn().get(registry_key(merchant_id, customer_phone))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return BatchState.from_json(json.loads(raw))

    def take(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> BatchState | None:
        raw = self._conn().eval(
            _LUA_TAKE, 1, registry_key(merchant_id, customer_phone)
        )
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return BatchState.from_json(json.loads(raw))

    def clear(self, merchant_id: uuid.UUID, customer_phone: str) -> None:
        self._conn().delete(registry_key(merchant_id, customer_phone))

    def acquire_lock(
        self,
        merchant_id: uuid.UUID,
        customer_phone: str,
        *,
        ttl: int = DEFAULT_LOCK_TTL_SECONDS,
    ) -> bool:
        return bool(
            self._conn().set(
                lock_key(merchant_id, customer_phone),
                "1",
                nx=True,
                ex=ttl,
            )
        )

    def release_lock(
        self, merchant_id: uuid.UUID, customer_phone: str
    ) -> None:
        self._conn().delete(lock_key(merchant_id, customer_phone))


_backend: BatchBackend | None = None
_scheduler = None


def get_backend() -> BatchBackend:
    global _backend
    if _backend is None:
        _backend = RedisBatchBackend()
    return _backend


def set_backend(backend: BatchBackend | None) -> None:
    global _backend
    _backend = backend


def set_scheduler(scheduler) -> None:
    """Tests replace RQ enqueue with an in-process callback `(mid, phone, token, pnid, delay)`."""
    global _scheduler
    _scheduler = scheduler


def register_pending(
    merchant_id: uuid.UUID,
    customer_phone: str,
    entry: PendingEntry,
    *,
    phone_number_id: str,
    now: float | None = None,
) -> BatchState:
    backend = get_backend()
    moment = now_seconds() if now is None else now
    entry.seq = backend.next_seq(merchant_id, customer_phone)
    token = str(uuid.uuid4())
    state = backend.append(
        merchant_id,
        customer_phone,
        entry,
        now=moment,
        token=token,
        phone_number_id=phone_number_id,
    )
    logger.info(
        "Registered pending WhatsApp %s message_id=%s seq=%s count=%s",
        entry.kind,
        truncate_wamid(entry.wamid or ""),
        entry.seq,
        len(state.entries),
    )
    return state


def schedule_conversation_flush(
    merchant_id: uuid.UUID,
    customer_phone: str,
    token: str,
    phone_number_id: str,
    *,
    delay: float,
) -> None:
    """Enqueue a delayed flush. Failed schedule falls back to immediate."""
    if _scheduler is not None:
        _scheduler(merchant_id, customer_phone, token, phone_number_id, delay)
        return

    from datetime import timedelta

    from redis import Redis
    from rq import Queue

    from app.workers.whatsapp import flush_conversation

    queue = Queue("whatsapp", connection=Redis.from_url(settings.redis_url))
    args = (str(merchant_id), customer_phone, token, phone_number_id)
    try:
        if delay <= 0:
            queue.enqueue(flush_conversation, *args)
        else:
            queue.enqueue_in(timedelta(seconds=delay), flush_conversation, *args)
    except Exception:
        logger.exception(
            "Flush schedule failed merchant=%s; enqueueing immediately",
            merchant_id,
        )
        try:
            queue.enqueue(flush_conversation, *args)
        except Exception:
            logger.exception(
                "Immediate flush enqueue also failed merchant=%s; running inline",
                merchant_id,
            )
            flush_conversation(*args)
