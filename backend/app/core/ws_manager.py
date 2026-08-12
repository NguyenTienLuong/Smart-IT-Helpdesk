"""Quản lý kết nối WebSocket + cầu nối Redis pub/sub.

★ ĐÂY LÀ CHỖ TRẢ LỜI TRỰC TIẾP MỐI LO CỦA ADR-0008 (đã được ghi đè bởi
ADR-0009): worker/API instance A tạo ra thông báo, nhưng người nhận có thể
đang giữ kết nối WebSocket ở instance B. Giải quyết bằng hai tầng:

1. **Giao local**: instance nào đang giữ kết nối của user thì gửi thẳng qua
   `asyncio.run_coroutine_threadsafe` — cần vì `NotificationService` là code
   ĐỒNG BỘ (chạy trong threadpool của FastAPI hoặc trong Celery worker),
   không thể `await websocket.send_json()` trực tiếp từ đó.
2. **Phát Redis pub/sub**: để các instance KHÁC (đang giữ kết nối của cùng
   user) cũng nhận được. Tự đánh dấu `origin` bằng UUID sinh ra lúc tiến
   trình khởi động, để một instance không tự phát rồi tự nhận lại lần hai từ
   Redis, gây gửi trùng cho client.

Giống `reports/cache.py`: Redis là TỐI ƯU cho multi-instance, không phải
PHỤ THUỘC — nếu Redis chết, giao local trong cùng tiến trình vẫn hoạt động
bình thường (đúng kịch bản triển khai một instance, như trong docker-compose
dự án này và trong test).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import WebSocket

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

REDIS_CHANNEL = "ws:notifications"
_INSTANCE_ID = uuid.uuid4().hex


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._redis_task: asyncio.Task | None = None
        self._pub_client: Any | None = None
        self._pub_retry_after: float = 0.0

    # ── Vòng đời (gọi từ lifespan trong main.py) ────────────────────────

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def start_redis_listener(self) -> None:
        """Chạy nền, lắng Redis để nhận sự kiện phát ra từ instance khác.

        Không có Redis thì bỏ qua lặng lẽ — xem docstring module."""
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
            pubsub = client.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
        except Exception as exc:
            logger.info(f"ws: Redis pub/sub không dùng được, chỉ giao local ({type(exc).__name__})")
            return

        async def _loop() -> None:
            try:
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        envelope = json.loads(message["data"])
                    except (TypeError, ValueError):
                        continue
                    if envelope.get("origin") == _INSTANCE_ID:
                        continue  # chính mình vừa phát — đã giao local rồi
                    await self._deliver_local(UUID(envelope["user_id"]), envelope["payload"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - phòng vệ, không nên tới đây
                logger.warning(f"ws: mất kết nối Redis pub/sub — {exc}")
            finally:
                await pubsub.close()
                await client.close()

        self._redis_task = asyncio.create_task(_loop())

    async def stop_redis_listener(self) -> None:
        if self._redis_task is not None:
            self._redis_task.cancel()
            self._redis_task = None

    # ── Kết nối ─────────────────────────────────────────────────────────

    def register(self, user_id: UUID, ws: WebSocket) -> None:
        self._connections[user_id].add(ws)

    def unregister(self, user_id: UUID, ws: WebSocket) -> None:
        conns = self._connections.get(user_id)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            self._connections.pop(user_id, None)

    async def _deliver_local(self, user_id: UUID, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(user_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister(user_id, ws)

    # ── Gửi (gọi từ code ĐỒNG BỘ — NotificationService, Celery task) ────

    def notify_sync(self, user_id: UUID, payload: dict) -> None:
        """Fire-and-forget: không chờ, không raise — một client đang mất kết
        nối không được phép làm hỏng luồng nghiệp vụ đang tạo thông báo."""
        if self.loop is not None:
            with contextlib.suppress(RuntimeError):  # loop đã đóng (lúc tắt app) — bỏ qua
                asyncio.run_coroutine_threadsafe(self._deliver_local(user_id, payload), self.loop)

        self._publish_redis(user_id, payload)

    def _get_pub_client(self) -> Any | None:
        if self._pub_client is not None:
            return self._pub_client

        now = time.monotonic()
        if now < self._pub_retry_after:
            return None  # vẫn trong thời gian nghỉ sau lần hỏng gần nhất

        try:
            import redis

            client = redis.Redis.from_url(
                settings.REDIS_URL, socket_connect_timeout=0.2, socket_timeout=0.2
            )
            client.ping()
            self._pub_client = client
            return client
        except Exception:
            # ★ Cooldown thay vì thử lại ở MỌI lần gọi: `notify()` chạy trên
            # đường nóng của gần như mọi hành động nghiệp vụ (giao ticket,
            # bình luận, đổi trạng thái...). `pytest tests/unit` cố tình chạy
            # KHÔNG CÓ Redis (xem Makefile: "nhanh, không cần DB") — không có
            # cooldown thì mỗi test gọi `notify()` phải trả giá bằng một lần
            # thử kết nối, cả bộ unit test chậm hẳn đi vì một thứ hạ tầng nó
            # không cần tới. 5 giây đủ để Redis thật hồi phục sau sự cố mà
            # không đánh đổi hiệu năng đường nóng.
            self._pub_retry_after = now + 5.0
            return None

    def _publish_redis(self, user_id: UUID, payload: dict) -> None:
        client = self._get_pub_client()
        if client is None:
            return
        try:
            envelope = json.dumps(
                {"origin": _INSTANCE_ID, "user_id": str(user_id), "payload": payload},
                default=str,
                ensure_ascii=False,
            )
            client.publish(REDIS_CHANNEL, envelope)
        except Exception as exc:
            logger.debug(f"ws: phát Redis hỏng — {type(exc).__name__}: {exc}")
            self._pub_client = None
            self._pub_retry_after = time.monotonic() + 5.0


ws_manager = ConnectionManager()

