"""Unit test cho `ConnectionManager` (ADR-0009).

Không đụng DB, không đụng Redis thật — verify đúng phần logic của riêng
module này: đăng ký/huỷ đăng ký kết nối, giao local, dọn kết nối chết, và
cooldown khi Redis không dùng được (xem comment trong `_get_pub_client`).
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from app.core.ws_manager import ConnectionManager


class FakeWebSocket:
    """Giả lập tối thiểu — chỉ cần `send_json`."""

    def __init__(self, hong: bool = False) -> None:
        self.hong = hong
        self.da_nhan: list[dict] = []

    async def send_json(self, data: dict) -> None:
        if self.hong:
            raise RuntimeError("kết nối đã đóng")
        self.da_nhan.append(data)


@pytest.fixture
async def manager() -> ConnectionManager:
    m = ConnectionManager()
    m.bind_loop(asyncio.get_running_loop())
    return m


class TestDangKyVaGiaoLocal:
    async def test_giao_dung_nguoi_khong_giao_nham_nguoi_khac(self, manager: ConnectionManager):
        toi, nguoi_khac = uuid4(), uuid4()
        ws_toi = FakeWebSocket()
        ws_nguoi_khac = FakeWebSocket()
        manager.register(toi, ws_toi)
        manager.register(nguoi_khac, ws_nguoi_khac)

        await manager._deliver_local(toi, {"event": "notification:new"})

        assert ws_toi.da_nhan == [{"event": "notification:new"}]
        assert ws_nguoi_khac.da_nhan == []

    async def test_mot_nguoi_nhieu_thiet_bi_ca_hai_deu_nhan(self, manager: ConnectionManager):
        """Một user mở 2 tab/thiết bị — cả hai kết nối đều phải nhận."""
        toi = uuid4()
        tab1, tab2 = FakeWebSocket(), FakeWebSocket()
        manager.register(toi, tab1)
        manager.register(toi, tab2)

        await manager._deliver_local(toi, {"event": "notification:new"})

        assert tab1.da_nhan and tab2.da_nhan

    async def test_gui_toi_nguoi_khong_co_ket_noi_khong_loi(self, manager: ConnectionManager):
        await manager._deliver_local(uuid4(), {"event": "notification:new"})  # không raise

    async def test_ket_noi_hong_tu_dong_bi_don_khoi_danh_sach(self, manager: ConnectionManager):
        toi = uuid4()
        ws_hong = FakeWebSocket(hong=True)
        manager.register(toi, ws_hong)

        await manager._deliver_local(toi, {"event": "notification:new"})

        assert ws_hong not in manager._connections.get(toi, set())

    def test_huy_dang_ky_xoa_dung_ket_noi(self, manager: ConnectionManager):
        toi = uuid4()
        ws = FakeWebSocket()
        manager.register(toi, ws)

        manager.unregister(toi, ws)

        assert toi not in manager._connections

    def test_huy_dang_ky_nguoi_chua_tung_dang_ky_khong_loi(self, manager: ConnectionManager):
        manager.unregister(uuid4(), FakeWebSocket())  # không raise


class TestNotifySync:
    async def test_notify_sync_khong_co_loop_khong_loi(self):
        """`loop=None` là trạng thái trước khi app khởi động xong — gọi vẫn
        phải an toàn, không được raise làm hỏng luồng nghiệp vụ."""
        manager = ConnectionManager()
        manager.notify_sync(uuid4(), {"event": "notification:new"})  # không raise

    async def test_notify_sync_giao_toi_ket_noi_local(self, manager: ConnectionManager):
        toi = uuid4()
        ws = FakeWebSocket()
        manager.register(toi, ws)

        manager.notify_sync(toi, {"event": "notification:new", "data": {"id": "1"}})
        await asyncio.sleep(0.05)  # `run_coroutine_threadsafe` lập lịch, không chạy ngay

        assert ws.da_nhan == [{"event": "notification:new", "data": {"id": "1"}}]


class TestRedisCooldown:
    """Không có Redis thật trong unit test (xem Makefile: `test-unit` chạy
    không cần hạ tầng) — verify KHÔNG thử kết nối lại liên tục, để `notify()`
    trên đường nóng không bị chậm đi vì một thứ hạ tầng không tồn tại."""

    def test_khong_thu_lai_ngay_sau_khi_hong(self, manager: ConnectionManager):
        manager._pub_retry_after = time.monotonic() + 5.0  # giả lập vừa hỏng

        client = manager._get_pub_client()

        assert client is None  # còn trong cooldown, không được thử kết nối lại

    def test_het_cooldown_thi_thu_lai_va_dat_cooldown_moi_neu_van_hong(
        self, manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch
    ):
        """Ép kết nối Redis luôn hỏng (không phụ thuộc môi trường CI có Redis
        hay không) để test tất định: hết cooldown thì có thử lại, và nếu vẫn
        hỏng thì tự đặt cooldown mới — không rơi vào vòng lặp thử vô hạn."""
        import redis

        def _luon_hong(*args, **kwargs):
            raise ConnectionError("giả lập Redis không dùng được")

        monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_luon_hong))
        manager._pub_retry_after = time.monotonic() - 1.0  # cooldown đã qua

        client = manager._get_pub_client()

        assert client is None
        assert manager._pub_retry_after > time.monotonic()