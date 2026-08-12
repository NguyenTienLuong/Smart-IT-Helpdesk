"""Endpoint thông báo (US-36).

Mọi endpoint ở đây chỉ thao tác trên thông báo của CHÍNH người đang đăng
nhập — `user_id` được đưa thẳng vào mệnh đề WHERE ở repository, không phải
kiểm tra bằng một câu `if` ở tầng route.

★ REALTIME (ADR-0009, ghi đè ADR-0008): kênh đẩy sự kiện là `/ws` bên dưới.
Các endpoint REST (list/unread-count/mark-read) vẫn giữ nguyên — WebSocket
chỉ thay cho việc *polling định kỳ*, không thay cho việc đọc dữ liệu ban đầu
khi trang vừa tải hoặc khi kết nối WS chưa kịp mở.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams, page_params
from app.core.security import decode_access_token
from app.core.ws_manager import ws_manager
from app.db.session import get_db
from app.modules.notifications.schemas import (
    MarkReadResponse,
    NotificationResponse,
    UnreadCountResponse,
)
from app.modules.notifications.service import NotificationService
from app.modules.users.models import User
from app.modules.users.repository import UserRepository

logger = get_logger(__name__)

router = APIRouter()


def get_notification_service(db: Session = Depends(get_db)) -> NotificationService:
    return NotificationService(db)


@router.get("", response_model=Page[NotificationResponse], summary="Thông báo của tôi")
def list_notifications(
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    params: PageParams = Depends(page_params),
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> Page[NotificationResponse]:
    items, total = service.list_mine(current_user.id, params, unread_only=unread_only)
    return Page.create([NotificationResponse.model_validate(n) for n in items], total, params)


@router.get(
    "/unread-count",
    response_model=UnreadCountResponse,
    summary="Đếm thông báo chưa đọc (snapshot lúc tải trang / kết nối WS chưa kịp mở)",
)
def unread_count(
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> UnreadCountResponse:
    """Không còn bị gọi 30 giây/lần (ADR-0009 thay bằng WebSocket) — chỉ gọi
    một lần lúc mount và làm phao dự phòng nếu WS chưa kết nối được. Vẫn giữ
    rẻ như cũ — chỉ một `COUNT` chạy trên partial index."""
    return UnreadCountResponse(count=service.unread_count(current_user.id))


@router.post(
    "/{notification_id}/read",
    response_model=MarkReadResponse,
    summary="Đánh dấu một thông báo đã đọc",
)
def mark_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> MarkReadResponse:
    updated = service.mark_read(current_user.id, notification_id)
    # 0 dòng có hai nguyên nhân: thông báo của người khác, hoặc đã đọc rồi.
    # Phân biệt bằng một lần đọc — và trả 404 cho trường hợp đầu, KHÔNG phải
    # 403: 403 tự xác nhận thông báo đó có tồn tại.
    if updated == 0 and service.notifications.get_owned(current_user.id, notification_id) is None:
        raise NotFoundError("Không tìm thấy thông báo")
    return MarkReadResponse(updated=updated)


@router.post("/read-all", response_model=MarkReadResponse, summary="Đánh dấu tất cả đã đọc")
def mark_all_read(
    current_user: User = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> MarkReadResponse:
    return MarkReadResponse(updated=service.mark_all_read(current_user.id))


@router.websocket("/ws")
async def notifications_ws(
    websocket: WebSocket,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> None:
    """Kênh đẩy realtime (ADR-0009). Xác thực bằng `?token=` trên URL —

    trình duyệt KHÔNG cho gắn header tuỳ ý vào lúc bắt tay WebSocket, đây là
    lý do duy nhất token phải nằm trên query string thay vì header Bearer
    như REST. Đánh đổi: token có thể lộ vào access log của proxy/CDN đứng
    trước — chấp nhận được vì đây là access token sống ngắn hạn, không phải
    refresh token.
    """
    try:
        payload = decode_access_token(token)
        user_id = UUID(payload["sub"])
    except Exception:
        await websocket.close(code=4401)
        return

    user = UserRepository(db).get_by_id(user_id)
    if user is None or not user.is_active:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    ws_manager.register(user.id, websocket)
    logger.debug("ws: kết nối mới", extra={"extra_fields": {"user_id": str(user.id)}})
    try:
        while True:
            # Không cần đọc gì từ client — chỉ chờ để phát hiện lúc client
            # đóng kết nối (WebSocketDisconnect). Client có thể gửi ping
            # riêng để giữ kết nối qua proxy timeout; ta bỏ qua nội dung.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.unregister(user.id, websocket)