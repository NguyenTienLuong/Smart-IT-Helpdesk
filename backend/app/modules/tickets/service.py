"""TicketService — vòng đời ticket (US-08 → US-18).

Service này KHÔNG tự viết lại quy tắc: nó điều phối ba lớp thuần đã có sẵn.

    TicketStateMachine  — bước chuyển trạng thái nào hợp lệ, ai được làm
    TicketAccessPolicy  — ai nhìn thấy / sửa được ticket nào
    SlaCalculator       — hạn SLA theo giờ hành chính

Ba lớp đó không chạm database nên test được trong mili-giây và đã phủ hết
các nhánh. Nếu logic bị chép lại vào đây, hai bản sẽ trôi khác nhau và bản
trong service — bản thật sự chạy — là bản không ai test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidStatusTransitionError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.modules.notifications.service import (
    ENTITY_TICKET,
    NotificationService,
    assigned_message,
    commented_message,
    rating_requested_message,
    resolved_message,
    status_message,
)
from app.modules.tickets.constants import (
    ActorType,
    AiStatus,
    EventType,
    SlaState,
    TicketPriority,
    TicketSource,
    TicketStatus,
)
from app.modules.tickets.models import (
    AiClassification,
    SlaPolicy,
    Ticket,
    TicketComment,
    TicketEvent,
)
from app.modules.tickets.policies import TicketAccessPolicy
from app.modules.tickets.repository import (
    CommentRepository,
    TicketEventRepository,
    TicketRepository,
)
from app.modules.tickets.schemas import (
    ChangeStatusRequest,
    CreateTicketRequest,
    UpdateTicketRequest,
)
from app.modules.tickets.sla import BusinessCalendar, SlaCalculator
from app.modules.tickets.state_machine import TicketStateMachine
from app.modules.tickets.suggestions import AssigneeSuggestionService, ScoredAgent
from app.modules.users.constants import UserRole
from app.modules.users.models import Holiday, User

logger = get_logger(__name__)


class TicketService:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.tickets = TicketRepository(session)
        self.comments = CommentRepository(session)
        self.events = TicketEventRepository(session)
        # Thông báo được ghi trong CÙNG transaction với thay đổi sinh ra nó.
        # Gửi sau khi commit thì có lúc thay đổi thành công mà thông báo mất;
        # gửi trước thì có lúc báo một việc rồi rollback. Cùng transaction là
        # cách duy nhất hai thứ không bao giờ lệch nhau (ADR-0007).
        self.notifier = NotificationService(session)
        self._sla: SlaCalculator | None = None

    # ── US-08: Tạo ticket ─────────────────────────────────────────────

    def create(self, user: User, data: CreateTicketRequest) -> Ticket:
        priority = data.priority or self._default_priority(data.category_id)

        ticket = Ticket(
            code=self.tickets.next_code(),
            title=data.title.strip(),
            description=data.description.strip(),
            status=TicketStatus.NEW,
            priority=priority,
            requester_id=user.id,
            category_id=data.category_id,
            department_id=user.department_id,
            source=TicketSource.CHATBOT if data.chat_session_id else TicketSource.WEB,
            chat_session_id=data.chat_session_id,
        )
        self._apply_sla(ticket)
        self.tickets.add(ticket)

        self._record(ticket, user, EventType.CREATED, new_value=ticket.code)
        self.db.commit()

        self._enqueue_classification(ticket)

        logger.info(
            "tạo ticket",
            extra={
                "extra_fields": {
                    "ticket_id": str(ticket.id),
                    "code": ticket.code,
                    "priority": priority,
                }
            },
        )
        return self._load(user, ticket.id)

    def _enqueue_classification(self, ticket: Ticket) -> None:
        """Đẩy việc phân loại sang worker (US-19) — BẤT ĐỒNG BỘ, SAU khi commit.

        Gọi LLM đồng bộ ngay tại đây thì mỗi lần tạo ticket phải chờ 5–20 giây
        (vi phạm NFR p95 < 500 ms), và LLM hỏng sẽ thành "không tạo được
        ticket" — đúng lúc cần hệ thống nhất thì nó lại chết.

        ★ try/except bao trọn là BẮT BUỘC, không phải phòng thủ thừa: Redis
        chết mà việc tạo ticket cũng chết theo là vi phạm BR-15. Việc bị rơi
        sẽ được `reconcile_pending` nhặt lại trong 10 phút — lưới an toàn thay
        cho transactional outbox (ADR-0007).

        ★ `publish_connection()` + `retry=False` cũng BẮT BUỘC. Đo thực tế
        với Redis không kết nối được, `POST /tickets` mất **19,3 giây** — ticket
        vẫn tạo được nhưng NFR p95 < 500 ms thì tan tành. Xem chú thích ở
        `app/celery_app.py` về hai điểm treo tách biệt đã tìm ra.

        Import cục bộ để tránh vòng lặp import: tasks → classifier → service.
        """
        try:
            from app.celery_app import publish_connection
            from app.modules.tickets.tasks import classify_ticket

            with publish_connection() as connection:
                classify_ticket.apply_async(
                    args=[str(ticket.id)], retry=False, connection=connection
                )
        except Exception as exc:
            logger.warning(
                f"không xếp hàng được việc phân loại, để job đối soát nhặt lại: {exc}",
                extra={"extra_fields": {"ticket_id": str(ticket.id)}},
            )

    # ── US-10, US-12, US-16: Danh sách, hàng chờ, tìm kiếm ────────────

    def list(
        self,
        user: User,
        params: PageParams,
        *,
        status: list[TicketStatus] | None = None,
        priority: list[TicketPriority] | None = None,
        category_id: UUID | None = None,
        assignee_id: UUID | None = None,
        mine: bool = False,
        unassigned: bool = False,
        q: str | None = None,
        sort_by: str = "createdAt",
        sort_order: str = "desc",
    ) -> tuple[list[Ticket], int]:
        return self.tickets.list(
            params,
            visible=TicketAccessPolicy.visible_filter(user),
            status=status,
            priority=priority,
            category_id=category_id,
            assignee_id=user.id if mine else assignee_id,
            unassigned=unassigned,
            q=q,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    def queue_stats(self, user: User) -> dict[str, int]:
        if not TicketAccessPolicy.can_assign(user):
            raise ForbiddenError("Chỉ IT Agent và Admin xem được hàng chờ")
        return self.tickets.queue_stats(user.id, datetime.now(UTC))

    # ── US-11: Chi tiết ───────────────────────────────────────────────

    def get(self, user: User, ticket_id: UUID) -> Ticket:
        return self._load(user, ticket_id)

    def allowed_transitions(self, user: User, ticket_id: UUID) -> list[TicketStatus]:
        ticket = self._load(user, ticket_id)
        return TicketStateMachine.allowed_next(
            ticket.status,
            user.role,
            is_requester=ticket.requester_id == user.id,
            is_assignee=ticket.assignee_id == user.id,
        )

    # ── Sửa nội dung ──────────────────────────────────────────────────

    def update(self, user: User, ticket_id: UUID, data: UpdateTicketRequest) -> Ticket:
        ticket = self._load(user, ticket_id)
        self._check_version(ticket, data.version)

        if not TicketAccessPolicy.can_edit(user, ticket):
            raise ForbiddenError("Chỉ sửa được ticket của mình khi còn ở trạng thái NEW")

        # ★ Nhân viên chỉ được sửa TIÊU ĐỀ và MÔ TẢ (tài liệu 06 §5).
        #
        # `can_edit` trả True/False cho cả bản ghi, còn `UpdateTicketRequest`
        # có sẵn `priority` và `categoryId` — nên bản trước cho nhân viên tự
        # đẩy ticket của mình lên URGENT. Hậu quả kép: `_apply_sla` tính lại
        # hạn theo mức mới nên người đó nhảy lên đầu hàng chờ, và sửa
        # `categoryId` còn kích hoạt `_record_ai_correction`, tức người dùng
        # TỰ ĐÁNH DẤU AI phân loại sai và bóp méo báo cáo US-22.
        #
        # Mức ưu tiên là quyết định của đội IT sau khi đánh giá tác động, không
        # phải điều người gửi yêu cầu tự khai.
        if user.role == UserRole.EMPLOYEE and (
            data.priority is not None or data.category_id is not None
        ):
            raise ForbiddenError(
                "Bạn chỉ sửa được tiêu đề và mô tả. "
                "Mức ưu tiên và loại sự cố do đội IT quyết định.",
                details={"editableFields": ["title", "description"]},
            )

        changed = False
        if data.title is not None and data.title.strip() != ticket.title:
            ticket.title = data.title.strip()
            changed = True
        if data.description is not None and data.description.strip() != ticket.description:
            ticket.description = data.description.strip()
            changed = True
        if data.category_id is not None and data.category_id != ticket.category_id:
            self._record(
                ticket,
                user,
                EventType.RECLASSIFIED,
                field_name="category_id",
                old_value=str(ticket.category_id or ""),
                new_value=str(data.category_id),
            )
            ticket.category_id = data.category_id
            self._record_ai_correction(ticket, data.category_id)
            changed = True
        if data.priority is not None and data.priority != ticket.priority:
            self._record(
                ticket,
                user,
                EventType.PRIORITY_CHANGED,
                field_name="priority",
                old_value=ticket.priority,
                new_value=data.priority,
            )
            ticket.priority = data.priority
            # Hạn SLA được tính lại theo mức ưu tiên MỚI, nhưng vẫn tính từ
            # thời điểm TẠO ticket — không phải từ bây giờ. Tính lại từ bây giờ
            # là tự gia hạn cho mình mỗi lần đổi mức ưu tiên.
            self._apply_sla(ticket)
            changed = True

        if changed:
            self.db.commit()
        return self._load(user, ticket_id)

    # ── US-13: Nhận việc / giao việc ──────────────────────────────────

    def claim(self, user: User, ticket_id: UUID, version: int) -> Ticket:
        """IT Agent tự nhận ticket chưa có người xử lý."""
        if not TicketAccessPolicy.can_claim(user):
            raise ForbiddenError("Chỉ IT Agent mới tự nhận ticket")

        ticket = self._load(user, ticket_id)
        self._check_version(ticket, version)

        if ticket.assignee_id is not None:
            # Đây chính là tình huống optimistic lock bảo vệ: hai Agent cùng
            # bấm "Nhận việc". Người thứ hai phải biết mình thua, chứ không
            # được âm thầm ghi đè người thứ nhất.
            raise ConflictError(
                "Ticket đã có người nhận",
                details={"assigneeId": str(ticket.assignee_id)},
            )

        return self._do_assign(user, ticket, user.id)

    def assign(self, user: User, ticket_id: UUID, assignee_id: UUID, version: int) -> Ticket:
        if not TicketAccessPolicy.can_assign(user):
            raise ForbiddenError("Bạn không có quyền giao việc")

        ticket = self._load(user, ticket_id)
        self._check_version(ticket, version)

        assignee = self.db.get(User, assignee_id)
        if assignee is None:
            raise NotFoundError("Không tìm thấy người được giao")
        # BR-02: chỉ IT Agent đang hoạt động mới nhận việc được. Giao cho một
        # nhân viên thường nghĩa là ticket rơi vào hố đen.
        if not assignee.can_be_assigned:
            raise ValidationError(
                "Chỉ giao được cho IT Agent đang hoạt động",
                details={"assigneeId": str(assignee_id)},
            )

        return self._do_assign(user, ticket, assignee_id)

    def _do_assign(self, user: User, ticket: Ticket, assignee_id: UUID) -> Ticket:
        # Dùng get_rule chứ không dùng validate(): giao lại một ticket đang ở
        # ASSIGNED là bước chuyển ASSIGNED → ASSIGNED, mà validate() từ chối
        # thẳng mọi trường hợp from == to. Bảng ALLOWED mới là nguồn sự thật,
        # và nó CÓ khai báo bước tự chuyển này (dòng "giao lại").
        rule = TicketStateMachine.get_rule(ticket.status, TicketStatus.ASSIGNED, user.role)
        if rule is None:
            raise InvalidStatusTransitionError(
                f"Không giao việc được khi ticket đang ở trạng thái {ticket.status}",
                details={
                    "currentStatus": ticket.status,
                    "allowedStatuses": TicketStateMachine.allowed_next(ticket.status, user.role),
                },
            )

        old_assignee = ticket.assignee_id
        old_status = ticket.status
        ticket.assignee_id = assignee_id
        ticket.status = TicketStatus.ASSIGNED

        if old_status != ticket.status:
            self._record(
                ticket,
                user,
                EventType.STATUS_CHANGED,
                field_name="status",
                old_value=old_status,
                new_value=ticket.status,
            )

        self._record(
            ticket,
            user,
            EventType.ASSIGNED,
            field_name="assignee_id",
            old_value=str(old_assignee) if old_assignee else None,
            new_value=str(assignee_id),
        )

        # US-34 — báo cho người vừa được giao. BR-18 lo phần "Agent tự nhận
        # việc thì không tự báo cho mình" nên ở đây không cần câu if nào.
        title, body = assigned_message(
            ticket.code, ticket.title, str(ticket.priority), ticket.sla_resolution_due_at
        )
        self.notifier.notify(
            user_id=assignee_id,
            notification_type="TICKET_ASSIGNED",
            title=title,
            body=body,
            entity_type=ENTITY_TICKET,
            entity_id=ticket.id,
            actor_id=user.id,
        )

        self.db.commit()
        return self._load(user, ticket.id)

    # ── US-14, US-18: Chuyển trạng thái / đóng / huỷ ──────────────────

    def change_status(self, user: User, ticket_id: UUID, data: ChangeStatusRequest) -> Ticket:
        ticket = self._load(user, ticket_id)
        self._check_version(ticket, data.version)

        old_status = ticket.status
        TicketStateMachine.validate(
            from_status=old_status,
            to_status=data.status,
            role=user.role,
            is_requester=ticket.requester_id == user.id,
            is_assignee=ticket.assignee_id == user.id,
            has_assignee=ticket.assignee_id is not None,
            resolution_note=data.resolution_note or ticket.resolution_note,
        )

        if old_status == TicketStatus.RESOLVED and data.status == TicketStatus.IN_PROGRESS:
            self._check_reopen_window(ticket)

        now = datetime.now(UTC)
        ticket.status = data.status

        if data.resolution_note:
            ticket.resolution_note = data.resolution_note.strip()

        # Ticket trở nên "có thể đánh giá" (RATEABLE_STATUSES ở feedback/service.py)
        # đúng một lần duy nhất: hoặc lúc chuyển sang RESOLVED, hoặc lúc bấm
        # thẳng CLOSED mà trước đó chưa từng qua RESOLVED. Nếu đã từng RESOLVED
        # rồi mới CLOSED thì lời mời đánh giá đã gửi từ lúc đó — không gửi lại.
        just_became_rateable = data.status == TicketStatus.RESOLVED or (
            data.status == TicketStatus.CLOSED and old_status != TicketStatus.RESOLVED
        )

        if data.status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
        elif data.status == TicketStatus.CLOSED:
            self._settle_ai_accuracy(ticket)
            ticket.closed_at = now
            # RESOLVED → CLOSED có thể do người dùng bấm ngay; nếu chưa từng
            # qua RESOLVED thì resolved_at vẫn phải có, do CHECK constraint.
            ticket.resolved_at = ticket.resolved_at or now
        elif old_status == TicketStatus.RESOLVED:
            # Mở lại: xoá dấu đã xử lý, nếu không báo cáo "thời gian xử lý
            # trung bình" (US-38) sẽ đếm ticket này là đã xong.
            ticket.resolved_at = None
            self._record(ticket, user, EventType.REOPENED)

        self._mark_first_response(ticket, user, now)

        self._record(
            ticket,
            user,
            EventType.STATUS_CHANGED,
            field_name="status",
            old_value=old_status,
            new_value=data.status,
        )
        self._notify_status_change(ticket, user, data.status, just_became_rateable)
        self.db.commit()

        logger.info(
            "đổi trạng thái ticket",
            extra={
                "extra_fields": {"ticket_id": str(ticket.id), "from": old_status, "to": data.status}
            },
        )
        return self._load(user, ticket_id)

    # ── US-15: Bình luận ──────────────────────────────────────────────

    def add_comment(
        self, user: User, ticket_id: UUID, body: str, is_internal: bool
    ) -> TicketComment:
        ticket = self._load(user, ticket_id)

        if not TicketAccessPolicy.can_comment(user, ticket):
            raise ForbiddenError("Bạn không có quyền bình luận trên ticket này")

        # Employee gửi is_internal=true thì BỎ QUA, không báo lỗi (BR-10).
        # Báo lỗi là tự khai rằng có tồn tại loại bình luận nội bộ.
        internal = is_internal and TicketAccessPolicy.can_use_internal_comment(user)

        comment = self.comments.add(
            TicketComment(
                ticket_id=ticket.id,
                author_id=user.id,
                body=body.strip(),
                is_internal=internal,
            )
        )

        # Bình luận nội bộ KHÔNG tính là đã phản hồi người dùng — người dùng
        # không nhìn thấy nó, tính vào là làm đẹp số liệu SLA một cách sai sự thật.
        if not internal:
            self._mark_first_response(ticket, user, datetime.now(UTC))

        self._record(
            ticket,
            user,
            EventType.COMMENTED,
            event_metadata={"commentId": str(comment.id), "isInternal": internal},
        )
        self._notify_comment(ticket, user, internal)
        self.db.commit()
        self.db.refresh(comment)
        return comment

    def list_comments(self, user: User, ticket_id: UUID) -> list[TicketComment]:
        self._load(user, ticket_id)  # kiểm tra quyền xem ticket trước
        return self.comments.list_for_ticket(
            ticket_id,
            include_internal=TicketAccessPolicy.can_see_internal_comments(user),
        )

    def update_comment(
        self, user: User, ticket_id: UUID, comment_id: UUID, body: str
    ) -> TicketComment:
        ticket = self._load(user, ticket_id)  # 404 nếu không có quyền xem (BR-09)

        if ticket.status == TicketStatus.CLOSED:
            raise ValidationError("Ticket đã đóng, không thể sửa bình luận")

        comment = self.comments.get_by_id(ticket_id, comment_id)
        if comment is None:
            raise NotFoundError("Không tìm thấy bình luận")

        if comment.author_id != user.id:
            raise ForbiddenError("Chỉ tác giả mới được sửa bình luận này")

        comment.body = body.strip()
        comment.edited_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(comment)
        return comment

    def delete_comment(self, user: User, ticket_id: UUID, comment_id: UUID) -> None:
        ticket = self._load(user, ticket_id)

        if ticket.status == TicketStatus.CLOSED:
            raise ValidationError("Ticket đã đóng, không thể xoá bình luận")

        comment = self.comments.get_by_id(ticket_id, comment_id)
        if comment is None:
            raise NotFoundError("Không tìm thấy bình luận")

        if comment.author_id != user.id and user.role != UserRole.ADMIN:
            raise ForbiddenError("Bạn không có quyền xoá bình luận này")

        self.comments.soft_delete(comment)
        self.db.commit()

    # ── US-17: Lịch sử thay đổi ───────────────────────────────────────

    def list_events(self, user: User, ticket_id: UUID) -> list[TicketEvent]:
        self._load(user, ticket_id)
        return self.events.list_for_ticket(ticket_id)

    # ── US-19: AI phân loại ghi vào ticket ────────────────────────────
    #
    # Hai phương thức dưới đây là ĐƯỜNG DUY NHẤT để AI chạm vào bảng tickets.
    # Chúng nằm ở đây chứ không nằm trong TicketClassifier vì mọi thay đổi
    # vòng đời ticket đều phải đi qua một chỗ — nếu không, quy tắc SLA và
    # nhật ký sự kiện sẽ có hai bản, và bản AI dùng là bản không ai test.

    def apply_ai_classification(
        self,
        ticket_id: UUID,
        *,
        category_id: UUID,
        priority: TicketPriority,
        reasoning: str,
        confidence: float,
    ) -> AiStatus:
        """Ghi kết quả phân loại của AI vào ticket. Trả về trạng thái đã chốt.

        ★ KHOÁ DÒNG (`with_for_update`) trước khi kiểm tra điều kiện. Giữa lúc
        gọi LLM (5–20 giây) và lúc ghi, Agent hoàn toàn có thể đã tự phân loại
        ticket. Đọc-rồi-ghi mà không khoá ở đây chính là ghi đè im lặng lên
        quyết định của con người — thứ mà BR-13 cấm.
        """
        ticket = self.db.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
        if ticket is None:
            return AiStatus.FAILED

        # Lượt phân loại khác đã chốt rồi — không đụng vào kết quả của nó.
        if ticket.ai_status != AiStatus.PENDING:
            return ticket.ai_status

        # BR-13 — AI KHÔNG BAO GIỜ ghi đè phân loại do con người chọn. Vẫn ghi
        # nhận gợi ý (bản ghi ai_classifications) để US-22 so sánh AI với người.
        #
        # ★ "Phân loại" gồm CẢ mức ưu tiên, không riêng loại sự cố. Bản đầu
        # tiên chỉ kiểm `category_id`, nên có kịch bản thật sau: nhân viên tạo
        # ticket không chọn loại; trong lúc LLM còn đang chạy, Agent xem qua và
        # tự đặt URGENT vì sự cố gấp; AI trả về MEDIUM và GHI ĐÈ — hạ mức ưu
        # tiên của con người xuống và nới hạn SLA thêm hai ngày. Nhật ký ghi
        # đúng hai dòng liên tiếp: USER MEDIUM→URGENT rồi AI URGENT→MEDIUM.
        if ticket.category_id is not None or self._nguoi_da_phan_loai(ticket.id):
            ticket.ai_status = AiStatus.SKIPPED
            self.db.flush()
            return AiStatus.SKIPPED

        old_priority = ticket.priority
        ticket.category_id = category_id
        ticket.priority = priority
        ticket.ai_status = AiStatus.APPLIED
        # Hạn SLA tính lại theo mức ưu tiên MỚI nhưng vẫn từ lúc TẠO ticket —
        # tính từ bây giờ là tự gia hạn cho mình mấy giây AI vừa tiêu tốn.
        self._apply_sla(ticket)

        self._record(
            ticket,
            None,
            EventType.AI_CLASSIFIED,
            actor_type=ActorType.AI,
            field_name="category_id",
            new_value=str(category_id),
            event_metadata={
                "confidence": round(confidence, 4),
                "reasoning": reasoning,
                "priority": str(priority),
            },
        )
        if old_priority != priority:
            self._record(
                ticket,
                None,
                EventType.PRIORITY_CHANGED,
                actor_type=ActorType.AI,
                field_name="priority",
                old_value=old_priority,
                new_value=priority,
            )
        self.db.flush()
        return AiStatus.APPLIED

    def mark_ai_status(self, ticket_id: UUID, status: AiStatus) -> None:
        """Chốt `ai_status` cho các nhánh KHÔNG áp dụng (LOW_CONFIDENCE, FAILED).

        Ticket giữ nguyên category/priority và nằm lại hàng chờ phân loại thủ
        công — đây là kết quả chấp nhận được, không phải sự cố.
        """
        ticket = self.db.get(Ticket, ticket_id, with_for_update=True, populate_existing=True)
        if ticket is None or ticket.ai_status != AiStatus.PENDING:
            return
        ticket.ai_status = status
        self.db.flush()

    def latest_ai_classification(self, user: User, ticket_id: UUID) -> AiClassification | None:
        """Gợi ý gần nhất của AI cho ticket. `_load` trước để kiểm tra quyền xem."""
        self._load(user, ticket_id)
        return self.db.execute(
            select(AiClassification)
            .options(selectinload(AiClassification.suggested_category))
            .where(AiClassification.ticket_id == ticket_id)
            .order_by(AiClassification.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ── US-20: Gợi ý người xử lý ──────────────────────────────────────

    def assignee_suggestions(
        self, user: User, ticket_id: UUID, *, now: datetime | None = None
    ) -> tuple[Ticket, list[ScoredAgent]]:
        if not TicketAccessPolicy.can_assign(user):
            raise ForbiddenError("Chỉ IT Agent và Admin xem được gợi ý người xử lý")

        ticket = self._load(user, ticket_id)
        service = AssigneeSuggestionService(self.db)
        return ticket, service.suggest(ticket, now=now or datetime.now(UTC))

    # ── US-21: Agent sửa lại phân loại của AI ─────────────────────────

    def _record_ai_correction(self, ticket: Ticket, corrected_category_id: UUID | None) -> None:
        """Đánh dấu bản ghi AI là bị sửa, để US-22 đo được độ chính xác thật.

        Chỉ tính khi AI THẬT SỰ đã áp dụng phân loại này (`was_applied`). AI
        chưa từng chạy, hoặc gợi ý đã bị bỏ qua vì độ tin cậy thấp, thì không
        có gì để tính là sai — tính vào sẽ kéo tỉ lệ chính xác xuống thấp hơn
        sự thật và làm cả chỉ số trở nên vô dụng.
        """
        record = self.db.execute(
            select(AiClassification)
            .where(
                AiClassification.ticket_id == ticket.id,
                AiClassification.was_applied.is_(True),
            )
            .order_by(AiClassification.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if record is None:
            return

        if record.suggested_category_id == corrected_category_id:
            # Agent đổi đi rồi đổi lại đúng loại AI đã chọn ⇒ hoàn tác việc
            # đánh dấu sai. Nếu không, một thao tác nhầm rồi sửa lại sẽ vĩnh
            # viễn bị đếm là "AI sai".
            record.was_accepted = None
            record.corrected_category_id = None
            record.corrected_at = None
            return

        record.was_accepted = False
        record.corrected_category_id = corrected_category_id
        record.corrected_at = datetime.now(UTC)

    def _settle_ai_accuracy(self, ticket: Ticket) -> None:
        """Ticket đóng mà không ai sửa ⇒ phân loại của AI được chấp nhận (US-21).

        Chốt sổ tại đây thay vì để `NULL` mãi: `was_accepted IS NULL` không
        phân biệt được "ticket chưa xong" với "không ai buồn sửa", mà báo cáo
        US-22 cần đúng sự phân biệt đó để tính mẫu số.
        """
        self.db.execute(
            update(AiClassification)
            .where(
                AiClassification.ticket_id == ticket.id,
                AiClassification.was_applied.is_(True),
                AiClassification.was_accepted.is_(None),
            )
            .values(was_accepted=True)
        )

    # ── US-14: Tự động đóng ticket đã xử lý xong ──────────────────────

    def auto_close_resolved(self, *, now: datetime | None = None) -> list[UUID]:
        """Đóng ticket RESOLVED quá `AUTO_CLOSE_AFTER_DAYS` ngày (job định kỳ).

        Không có bước này, ticket đã xử lý xong nằm mãi ở RESOLVED và mọi báo
        cáo "còn bao nhiêu việc đang mở" đều sai. Hành động của hệ thống nên
        `actor_id = NULL`, `actor_type = SYSTEM` (docs/design/02 US-17).
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(days=TicketStateMachine.AUTO_CLOSE_AFTER_DAYS)

        tickets = list(
            self.db.execute(
                select(Ticket).where(
                    Ticket.status == TicketStatus.RESOLVED,
                    Ticket.resolved_at.is_not(None),
                    Ticket.resolved_at <= cutoff,
                )
            )
            .scalars()
            .all()
        )

        for ticket in tickets:
            self._settle_ai_accuracy(ticket)
            ticket.status = TicketStatus.CLOSED
            ticket.closed_at = moment
            self._record(
                ticket,
                None,
                EventType.AUTO_CLOSED,
                field_name="status",
                old_value=TicketStatus.RESOLVED,
                new_value=TicketStatus.CLOSED,
                event_metadata={"afterDays": TicketStateMachine.AUTO_CLOSE_AFTER_DAYS},
            )

        if tickets:
            self.db.commit()
        return [t.id for t in tickets]

    # ── Nội bộ ────────────────────────────────────────────────────────

    def _load(self, user: User, ticket_id: UUID) -> Ticket:
        """Đọc ticket qua bộ lọc quyền.

        ★ Trả 404 chứ KHÔNG phải 403 khi ticket thuộc người khác. 403 là tự
        xác nhận "ticket này có tồn tại", cho phép dò mã ticket của công ty.
        """
        ticket = self.tickets.get(ticket_id, TicketAccessPolicy.visible_filter(user))
        if ticket is None:
            raise NotFoundError("Không tìm thấy ticket")
        return ticket

    def _nguoi_da_phan_loai(self, ticket_id: UUID) -> bool:
        """Đã có CON NGƯỜI can thiệp vào loại sự cố hoặc mức ưu tiên chưa?

        Hỏi nhật ký thay vì so sánh giá trị hiện tại: mức ưu tiên luôn khác
        `None` (mặc định MEDIUM), nên không có cách nào nhìn vào cột `priority`
        mà biết được nó do người đặt hay do hệ thống điền. `ticket_events` là
        chỉ-ghi-thêm và có `actor_type`, nên nó trả lời được câu hỏi này chính
        xác — và trả lời được cả sau này khi ticket đã qua tay nhiều người.
        """
        return (
            self.db.execute(
                select(func.count())
                .select_from(TicketEvent)
                .where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.actor_type == ActorType.USER,
                    TicketEvent.event_type.in_(
                        [EventType.PRIORITY_CHANGED, EventType.RECLASSIFIED]
                    ),
                )
            ).scalar_one()
            > 0
        )

    def _check_version(self, ticket: Ticket, version: int) -> None:
        """Khoá lạc quan (BR-16).

        Không có bước này thì hai Agent mở cùng một ticket, người sau bấm lưu
        sẽ ghi đè im lặng lên thay đổi của người trước — và không ai biết.
        """
        if ticket.version != version:
            raise ConflictError(
                "Ticket đã được người khác cập nhật. Vui lòng tải lại.",
                details={"currentVersion": ticket.version},
            )

    def _mark_first_response(self, ticket: Ticket, user: User, now: datetime) -> None:
        """Ghi nhận lần phản hồi đầu tiên của IT — mốc đo SLA phản hồi.

        Chỉ tính khi người thao tác KHÔNG phải người tạo ticket: người tạo tự
        bình luận thêm không phải là IT đã phản hồi.
        """
        if (
            ticket.first_response_at is None
            and user.id != ticket.requester_id
            and user.role in (UserRole.IT_AGENT, UserRole.ADMIN)
        ):
            ticket.first_response_at = now

    def _check_reopen_window(self, ticket: Ticket) -> None:
        if ticket.resolved_at is None:
            return
        days = (datetime.now(UTC) - ticket.resolved_at).days
        if days > TicketStateMachine.REOPEN_WINDOW_DAYS:
            raise ValidationError(
                f"Chỉ mở lại được trong {TicketStateMachine.REOPEN_WINDOW_DAYS} "
                f"ngày kể từ khi xử lý xong. Vui lòng tạo ticket mới."
            )

    def _record(
        self,
        ticket: Ticket,
        user: User | None,
        event_type: EventType,
        *,
        field_name: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        event_metadata: dict | None = None,
        actor_type: ActorType | None = None,
    ) -> None:
        """Ghi vào nhật ký chỉ-ghi-thêm (BR-17).

        `actor_type` chỉ cần truyền khi người thực hiện KHÔNG phải người dùng
        và cũng không phải hệ thống — hiện chỉ có AI (US-19). Mặc định suy ra
        từ việc có `user` hay không.
        """
        self.events.add(
            TicketEvent(
                ticket_id=ticket.id,
                actor_id=user.id if user else None,
                actor_type=actor_type or (ActorType.USER if user else ActorType.SYSTEM),
                event_type=event_type,
                field_name=field_name,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
                event_metadata=event_metadata or {},
            )
        )

    # ── Thông báo (F6) ────────────────────────────────────────────────

    def _notify_status_change(
        self,
        ticket: Ticket,
        actor: User,
        new_status: TicketStatus,
        just_became_rateable: bool = False,
    ) -> None:
        """US-33 — báo cho người yêu cầu và người xử lý khi trạng thái đổi.

        Người vừa bấm nút bị BR-18 loại ra, nên hai lời gọi dưới đây tự động
        chỉ tới đúng "bên còn lại" mà không cần so sánh id ở đây.

        `RESOLVED` được tách riêng: người yêu cầu cần lời mời xác nhận, còn
        người xử lý chỉ cần biết trạng thái đã đổi. Gửi cùng một câu cho cả
        hai sẽ mời chính Agent đi xác nhận việc mình vừa làm.
        """
        if new_status is TicketStatus.RESOLVED:
            title, body = resolved_message(ticket.code, ticket.title)
            self.notifier.notify(
                user_id=ticket.requester_id,
                notification_type="TICKET_RESOLVED",
                title=title,
                body=body,
                entity_type=ENTITY_TICKET,
                entity_id=ticket.id,
                actor_id=actor.id,
            )
            recipients = [ticket.assignee_id]
        else:
            recipients = [ticket.requester_id, ticket.assignee_id]

        title, body = status_message(ticket.code, ticket.title, str(new_status))
        self.notifier.notify_many(
            [r for r in recipients if r is not None],
            notification_type="TICKET_STATUS_CHANGED",
            title=title,
            body=body,
            entity_type=ENTITY_TICKET,
            entity_id=ticket.id,
            actor_id=actor.id,
        )

        # F6–F8: mời người yêu cầu đánh giá ngay khi ticket lần đầu tới
        # trạng thái có thể đánh giá được (RATEABLE_STATUSES). BR-18 vẫn áp
        # dụng qua notify(): nếu chính requester là người bấm đóng ticket
        # (self-service close) thì actor_id == user_id, không tự mời mình.
        if just_became_rateable:
            title, body = rating_requested_message(ticket.code, ticket.title)
            self.notifier.notify(
                user_id=ticket.requester_id,
                notification_type="RATING_REQUESTED",
                title=title,
                body=body,
                entity_type=ENTITY_TICKET,
                entity_id=ticket.id,
                actor_id=actor.id,
            )

    def _notify_comment(self, ticket: Ticket, actor: User, is_internal: bool) -> None:
        """US-33 — báo khi có bình luận mới, trừ bình luận của chính mình.

        ★ Bình luận nội bộ KHÔNG báo cho người yêu cầu (BR-10). Người yêu cầu
        không nhìn thấy nội dung bình luận đó, nên một thông báo "có phản hồi
        mới" dẫn tới một ticket không có gì mới vừa vô nghĩa vừa tự tố cáo
        rằng có tồn tại lớp bình luận ẩn.
        """
        recipients = [ticket.assignee_id]
        if not is_internal:
            recipients.append(ticket.requester_id)

        title, body = commented_message(ticket.code, ticket.title, actor.full_name)
        self.notifier.notify_many(
            [r for r in recipients if r is not None],
            notification_type="TICKET_COMMENTED",
            title=title,
            body=body,
            entity_type=ENTITY_TICKET,
            entity_id=ticket.id,
            actor_id=actor.id,
        )

    def _default_priority(self, category_id: UUID | None) -> TicketPriority:
        """Mức ưu tiên mặc định lấy từ loại sự cố, không phải hằng số cứng."""
        if category_id is None:
            return TicketPriority.MEDIUM
        from app.modules.tickets.models import TicketCategory

        category = self.db.get(TicketCategory, category_id)
        return category.default_priority if category else TicketPriority.MEDIUM

    def _apply_sla(self, ticket: Ticket) -> None:
        """SAO CHÉP hạn SLA vào ticket (BR-19).

        Không tính lại từ policy mỗi lần đọc: đổi chính sách SLA về sau sẽ làm
        thay đổi hạn của ticket cũ, và báo cáo lịch sử trở thành vô nghĩa.
        """
        policy = self.db.execute(
            select(SlaPolicy).where(SlaPolicy.priority == ticket.priority)
        ).scalar_one_or_none()
        if policy is None:
            logger.warning(f"chưa cấu hình SLA cho mức ưu tiên {ticket.priority}")
            return

        start = ticket.created_at or datetime.now(UTC)
        calculator = self._sla_calculator()
        ticket.sla_response_due_at = calculator.due_at(
            start, policy.first_response_minutes, policy.business_hours_only
        )
        ticket.sla_resolution_due_at = calculator.due_at(
            start, policy.resolution_minutes, policy.business_hours_only
        )

    def _sla_calculator(self) -> SlaCalculator:
        """Nạp ngày nghỉ MỘT lần cho mỗi request, không phải mỗi ticket."""
        if self._sla is None:
            holidays = frozenset(
                row.holiday_date.date() for row in self.db.execute(select(Holiday)).scalars().all()
            )
            # ★ Phải truyền giờ và múi giờ từ cấu hình. Bản trước dựng
            # `BusinessCalendar(holidays=...)` trần, nên đổi BUSINESS_HOUR_*
            # trong .env chỉ có tác dụng với gợi ý người xử lý mà KHÔNG có
            # tác dụng với SLA — hai chỗ nói hai điều khác nhau về cùng một
            # khái niệm "giờ hành chính".
            self._sla = SlaCalculator(
                BusinessCalendar(
                    start_hour=settings.BUSINESS_HOUR_START,
                    end_hour=settings.BUSINESS_HOUR_END,
                    holidays=holidays,
                    timezone=settings.BUSINESS_TIMEZONE,
                )
            )
        return self._sla

    def sla_state(self, ticket: Ticket, now: datetime | None = None) -> SlaState:
        return self._sla_calculator().state(
            created_at=ticket.created_at,
            due_at=ticket.sla_resolution_due_at,
            resolved_at=ticket.resolved_at,
            now=now or datetime.now(UTC),
            paused_seconds=ticket.paused_seconds,
        )
        
        