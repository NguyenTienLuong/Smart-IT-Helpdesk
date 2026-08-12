"""Kênh WebSocket của thông báo (ADR-0009 — ghi đè ADR-0008).

Cùng nguyên tắc với `test_notifications.py`: bám vào hành vi người dùng
thấy. Ở đây là "mở kết nối WS thì nhận được sự kiện ngay khi có chuyện xảy
ra", không phải chi tiết triển khai (Redis, threadsafe...).
"""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.modules.users.constants import UserRole
from tests.conftest import DEFAULT_TEST_PASSWORD


def tao_ticket(client, tieu_de="Máy in tầng 3 kẹt giấy liên tục"):
    response = client.post(
        "/api/v1/tickets",
        json={"title": tieu_de, "description": "Kẹt giấy mỗi lần in quá 5 trang, đã thử tắt bật."},
    )
    assert response.status_code == 201, response.text
    return response.json()


def lay_token(client, user) -> str:
    """Đăng nhập lấy access token thô — dùng cho query string của WS, khác
    với fixture `login` vốn gắn token vào header Authorization cho REST."""
    r = client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": DEFAULT_TEST_PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["accessToken"]


class TestKetNoi:
    def test_token_sai_bi_dong_ngay_khong_accept(self, client):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/notifications/ws?token=khong-hop-le"),
        ):
            pass

    def test_thieu_token_bi_tu_choi(self, client):
        # Không có `?token=` -> FastAPI trả 422 ngay ở tầng validate query,
        # bắt tay WS không thành công.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/notifications/ws"),
        ):
            pass


class TestDayThongBaoRealtime:
    def test_duoc_giao_ticket_thi_nhan_notification_new_qua_ws(
        self, client, make_user, login, sla_policies, ticket_category
    ):
        nhan_vien = make_user()
        agent = make_user(role=UserRole.IT_AGENT)
        admin = make_user(role=UserRole.ADMIN)

        ticket = tao_ticket(login(nhan_vien))
        token_agent = lay_token(client, agent)

        with client.websocket_connect(f"/api/v1/notifications/ws?token={token_agent}") as ws:
            c = login(admin)
            r = c.post(
                f"/api/v1/tickets/{ticket['id']}/assign",
                json={"assigneeId": str(agent.id), "version": ticket["version"]},
            )
            assert r.status_code == 200, r.text

            message = ws.receive_json()

        assert message["event"] == "notification:new"
        assert message["data"]["type"] == "TICKET_ASSIGNED"
        assert message["data"]["entityType"] == "ticket"
        assert message["data"]["entityId"] == ticket["id"]
        assert message["data"]["isRead"] is False

    def test_BR18_tu_nhan_viec_thi_KHONG_co_su_kien_nao_gui_qua_ws(
        self, client, make_user, login, sla_policies, ticket_category
    ):
        """Đối xứng với test REST cùng tên: BR-18 không tạo thông báo, nên
        cũng không có gì để đẩy qua WS. Verify bằng cách làm một hành động
        khác NGAY SAU đó và kiểm tra sự kiện nhận được là của hành động sau,
        không phải của việc tự nhận việc."""
        nhan_vien = make_user()
        agent = make_user(role=UserRole.IT_AGENT)
        admin = make_user(role=UserRole.ADMIN)

        ticket = tao_ticket(login(nhan_vien))
        token_agent = lay_token(client, agent)

        with client.websocket_connect(f"/api/v1/notifications/ws?token={token_agent}") as ws:
            c = login(agent)
            r = c.post(f"/api/v1/tickets/{ticket['id']}/claim", json={"version": ticket["version"]})
            assert r.status_code == 200, r.text

            # Không có gì để nhận từ việc tự nhận việc — tạo một sự kiện khác
            # (admin gán một ticket thứ hai) để chứng minh kết nối vẫn sống
            # và chỉ đúng MỘT sự kiện đó tới, không phải một sự kiện ẩn nào
            # từ thao tác claim ở trên.
            ticket_2 = tao_ticket(login(nhan_vien), "Sự cố thứ hai")
            c = login(admin)
            c.post(
                f"/api/v1/tickets/{ticket_2['id']}/assign",
                json={"assigneeId": str(agent.id), "version": ticket_2["version"]},
            )

            message = ws.receive_json()

        assert message["data"]["entityId"] == ticket_2["id"]

    def test_danh_dau_da_doc_phat_su_kien_notification_read(
        self, client, make_user, login, sla_policies, ticket_category
    ):
        nhan_vien = make_user()
        agent = make_user(role=UserRole.IT_AGENT)
        admin = make_user(role=UserRole.ADMIN)

        ticket = tao_ticket(login(nhan_vien))
        c = login(admin)
        c.post(
            f"/api/v1/tickets/{ticket['id']}/assign",
            json={"assigneeId": str(agent.id), "version": ticket["version"]},
        )

        c = login(agent)
        thong_bao = c.get("/api/v1/notifications").json()["data"][0]
        token_agent = lay_token(client, agent)

        with client.websocket_connect(f"/api/v1/notifications/ws?token={token_agent}") as ws:
            c = login(agent)
            r = c.post(f"/api/v1/notifications/{thong_bao['id']}/read")
            assert r.status_code == 200, r.text

            message = ws.receive_json()

        assert message["event"] == "notification:read"
        assert message["data"]["id"] == thong_bao["id"]

    def test_hai_thiet_bi_cung_mot_nguoi_ca_hai_deu_nhan(
        self, client, make_user, login, sla_policies, ticket_category
    ):
        """Multi-instance/multi-tab là chính lý do ADR-0008 từ chối WS lúc
        đầu — verify trực tiếp kịch bản đó: user mở 2 kết nối, cả hai đều
        phải thấy cùng một thông báo mới."""
        nhan_vien = make_user()
        agent = make_user(role=UserRole.IT_AGENT)
        admin = make_user(role=UserRole.ADMIN)

        ticket = tao_ticket(login(nhan_vien))
        token_agent = lay_token(client, agent)
        url = f"/api/v1/notifications/ws?token={token_agent}"

        with client.websocket_connect(url) as ws1, client.websocket_connect(url) as ws2:
            c = login(admin)
            c.post(
                f"/api/v1/tickets/{ticket['id']}/assign",
                json={"assigneeId": str(agent.id), "version": ticket["version"]},
            )

            msg1 = ws1.receive_json()
            msg2 = ws2.receive_json()

        assert msg1["data"]["id"] == msg2["data"]["id"]  # cùng một thông báo, gửi tới 2 kết nối
        assert msg1["data"]["entityId"] == ticket["id"]
        assert msg1["event"] == msg2["event"] == "notification:new"
        
        