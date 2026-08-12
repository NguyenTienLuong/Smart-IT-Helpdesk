"""NotificationService — sinh và đọc thông báo trong ứng dụng (F6).

★ MODULE NÀY KHÔNG BIẾT GÌ VỀ TICKET. Nó nhận dữ liệu nguyên thuỷ (mã ticket,
tiêu đề, trạng thái) và trả về thông báo. Lý do: `tickets/service.py` gọi
module này, còn `notifications/tasks.py` lại đọc bảng tickets — nếu service ở
đây cũng import model ticket thì thành vòng import, và vòng import chỉ nổ khi
chạy worker chứ không nổ trong test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.core.ws_manager import ws_manager
from app.modules.notifications.constants import NOTIFICATION_TYPES
from app.modules.notifications.models import Notification
from app.modules.notifications.repository import NotificationRepository

logger = get_logger(__name__)

ENTITY_TICKET = "ticket"


class NotificationService:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.notifications = NotificationRepository(session)

    # ── Ghi ───────────────────────────────────────────────────────────

    def notify(
        self,
        *,
        user_id: UUID | None,
        notification_type: str,
        title: str,
        body: str | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        actor_id: UUID | None = None,
        moment: datetime | None = None,
    ) -> Notification | None:
        """Tạo một thông báo. Trả về `None` khi cố tình không tạo.

        Hai lần "cố tình không tạo":

        1. **BR-18 — không tự thông báo cho chính người vừa hành động.** Agent
           bấm "Nhận việc" rồi nhận ngay thông báo "bạn được giao ticket" là
           tiếng ồn, và tiếng ồn làm người ta tắt thông báo.

        2. **Chống lặp trong `NOTIFY_DEDUP_MINUTES` phút.** US-33 viết là
           "gộp thành một", nhưng gộp thật chỉ làm được bằng hai cách và cách
           nào cũng vỡ: hoãn gửi 5 phút thì vi phạm cam kết ≤ 60 giây ở tài
           liệu 01 §7 và làm ADR-0008 (polling 30 giây) vô nghĩa; còn sửa bản
           ghi đã gửi thì chưa ai định nghĩa nó phải bật lại thành chưa đọc
           hay không. Nên ở đây **chặn bản sao** thay vì gộp: người nhận vẫn
           chỉ thấy một thông báo cho một chuỗi sự kiện cùng loại, mà thông
           báo đầu tiên tới ngay lập tức.

           Chống lặp theo BỘ BA (người nhận, loại, đối tượng) chứ không theo
           riêng ticket: "được giao việc" và "ticket đã xử lý xong" là hai
           tin khác nhau, chặn mất tin thứ hai là giấu thông tin.
        """
        if notification_type not in NOTIFICATION_TYPES:
            # Lỗi lập trình, không phải lỗi dữ liệu — CHECK constraint dưới DB
            # cũng sẽ chặn, nhưng chặn ở đây thì thấy được tên biến sai.
            raise ValueError(f"Loại thông báo không hợp lệ: {notification_type}")

        if user_id is None:
            return None

        if actor_id is not None and actor_id == user_id:
            return None  # BR-18

        now = moment or datetime.now(UTC)
        since = now - timedelta(minutes=settings.NOTIFY_DEDUP_MINUTES)
        if self.notifications.exists_recent(user_id, notification_type, entity_id, since):
            logger.debug(
                "bỏ qua thông báo trùng",
                extra={
                    "extra_fields": {
                        "user_id": str(user_id),
                        "type": notification_type,
                        "entity_id": str(entity_id) if entity_id else None,
                    }
                },
            )
            return None

        created = self.notifications.add(
            Notification(
                user_id=user_id,
                type=notification_type,
                title=title[:200],
                body=body[:500] if body else None,
                entity_type=entity_type,
                entity_id=entity_id,
                is_read=False,
            )
        )

        # ADR-0009: đẩy realtime qua WebSocket thay cho polling. `notify()`
        # chưa `commit()` (bên gọi tự quyết định lúc nào commit — xem
        # docstring đầu file), nên về lý thuyết có thể đẩy một thông báo mà
        # transaction sau đó rollback. Chấp nhận được: hệ quả tệ nhất là
        # client thấy một badge/chuông không khớp DB trong vài trăm ms, tự
        # sửa lại ở lần đồng bộ tiếp theo — không phải mất dữ liệu.
        ws_manager.notify_sync(user_id, {"event": "notification:new", "data": _ws_payload(created)})

        return created

    def notify_many(self, user_ids: list[UUID], **kwargs) -> int:
        """Gửi cùng một thông báo cho nhiều người (ví dụ toàn bộ Admin).

        Lọc trùng trước khi gửi: một người vừa là assignee vừa là Admin thì
        chỉ nhận một lần.
        """
        seen: set[UUID] = set()
        sent = 0
        for user_id in user_ids:
            if user_id is None or user_id in seen:
                continue
            seen.add(user_id)
            if self.notify(user_id=user_id, **kwargs) is not None:
                sent += 1
        return sent

    # ── Đọc (US-36) ───────────────────────────────────────────────────

    def list_mine(
        self, user_id: UUID, params: PageParams, *, unread_only: bool = False
    ) -> tuple[list[Notification], int]:
        return self.notifications.list_for_user(user_id, params, unread_only=unread_only)

    def unread_count(self, user_id: UUID) -> int:
        return self.notifications.count_unread(user_id)

    def mark_read(self, user_id: UUID, notification_id: UUID) -> int:
        updated = self.notifications.mark_read(user_id, notification_id, datetime.now(UTC))
        self.db.commit()
        if updated:
            # Đồng bộ badge/list ở các tab/thiết bị KHÁC của cùng user — tab
            # vừa gọi API này đã tự cập nhật UI từ response REST, không cần
            # nhận lại chính sự kiện của mình.
            ws_manager.notify_sync(
                user_id, {"event": "notification:read", "data": {"id": str(notification_id)}}
            )
        return updated

    def mark_all_read(self, user_id: UUID) -> int:
        updated = self.notifications.mark_all_read(user_id, datetime.now(UTC))
        self.db.commit()
        logger.info(
            "đánh dấu đã đọc tất cả",
            extra={"extra_fields": {"user_id": str(user_id), "count": updated}},
        )
        if updated:
            ws_manager.notify_sync(user_id, {"event": "notification:read_all", "data": {}})
        return updated


# ── Dựng payload WebSocket ─────────────────────────────────────────────


def _ws_payload(n: Notification) -> dict:
    """camelCase để khớp `NotificationResponse` phía REST — frontend dùng
    chung một kiểu dữ liệu cho cả hai nguồn, không phải viết hai lần."""
    return {
        "id": str(n.id),
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "entityType": n.entity_type,
        "entityId": str(n.entity_id) if n.entity_id else None,
        "isRead": n.is_read,
        "readAt": n.read_at.isoformat() if n.read_at else None,
        "createdAt": n.created_at.isoformat() if n.created_at else None,
    }


# ── Dựng nội dung thông báo ───────────────────────────────────────────
#
# Tách khỏi class để `tickets/service.py` gọi được mà không phải tự viết câu
# chữ. Toàn bộ tham số là kiểu nguyên thuỷ — xem lý do ở đầu file.


def assigned_message(
    code: str, title: str, priority: str, due_at: datetime | None
) -> tuple[str, str]:
    """US-34 — nội dung kèm mã ticket, tiêu đề, mức ưu tiên và hạn SLA."""
    han = f", hạn xử lý {due_at:%d/%m %H:%M}" if due_at else ""
    return (
        f"Bạn được giao ticket {code}",
        f"{title} — mức ưu tiên {priority}{han}",
    )


def status_message(code: str, title: str, new_status: str) -> tuple[str, str]:
    return (f"Ticket {code} đổi trạng thái", f"{title} — nay là {new_status}")


def resolved_message(code: str, title: str) -> tuple[str, str]:
    return (f"Ticket {code} đã được xử lý", f"{title} — mời bạn kiểm tra và xác nhận")


def commented_message(code: str, title: str, author_name: str) -> tuple[str, str]:
    return (f"Bình luận mới trên ticket {code}", f"{author_name} vừa phản hồi: {title}")


def sla_at_risk_message(code: str, title: str, due_at: datetime | None) -> tuple[str, str]:
    han = f" (hạn {due_at:%d/%m %H:%M})" if due_at else ""
    return (f"Ticket {code} sắp trễ hạn", f"{title}{han} — còn dưới 25% thời gian")


def sla_breached_message(code: str, title: str, due_at: datetime | None) -> tuple[str, str]:
    han = f" (hạn {due_at:%d/%m %H:%M})" if due_at else ""
    return (f"Ticket {code} ĐÃ TRỄ HẠN", f"{title}{han} — cần xử lý ngay")