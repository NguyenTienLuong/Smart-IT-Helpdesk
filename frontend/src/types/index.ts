/**
 * Kiểu dữ liệu dùng chung.
 *
 * ⚠️ File này viết TAY, phải tự khớp với schema của backend. Muốn đổi API mà
 * quên sửa frontend là LỖI LÚC BIÊN DỊCH thay vì lỗi lúc chạy thì cài lại
 * `openapi-typescript` và sinh từ openapi.json (task T17) — đã gỡ khỏi
 * package.json vì openapi.json chưa tồn tại, script chạy là hỏng.
 */

export type UserRole = "EMPLOYEE" | "IT_AGENT" | "ADMIN";

export type TicketStatus =
  | "NEW"
  | "ASSIGNED"
  | "IN_PROGRESS"
  | "PENDING_REQUESTER"
  | "RESOLVED"
  | "CLOSED"
  | "CANCELLED";

export type TicketPriority = "LOW" | "MEDIUM" | "HIGH" | "URGENT";
export type SlaState = "ON_TRACK" | "AT_RISK" | "BREACHED" | "MET";
export type AiStatus =
  | "PENDING"
  | "APPLIED"
  | "LOW_CONFIDENCE"
  | "SKIPPED"
  | "FAILED";

/** Người dùng khi xuất hiện lồng trong tài nguyên khác.
 *  KHÔNG có email — danh sách ticket không phải chỗ lộ email toàn công ty. */
export interface UserBrief {
  id: string;
  fullName: string;
  role: UserRole;
  avatarUrl?: string | null;
}

/** Thông tin đầy đủ, chỉ trả cho chính chủ hoặc Admin (GET /users/me). */
export interface CurrentUser extends UserBrief {
  email: string;
  department?: { id: string; code: string; name: string } | null;
  isActive: boolean;
  createdAt: string;
}

export interface Category {
  id: string;
  slug: string;
  name: string;
}

/** Bản rút gọn dùng cho danh sách — KHÔNG có `description`. */
export interface TicketListItem {
  id: string;
  code: string;
  title: string;
  status: TicketStatus;
  priority: TicketPriority;
  aiStatus: AiStatus;
  requester: UserBrief;
  assignee: UserBrief | null;
  category: Category | null;
  slaState: SlaState | null;
  slaResolutionDueAt: string | null;
  createdAt: string;
  updatedAt: string;
  version: number;
}

export interface Ticket extends TicketListItem {
  description: string;
  source: "WEB" | "CHATBOT" | "API";
  resolutionNote: string | null;
  slaResponseDueAt: string | null;
  firstResponseAt: string | null;
  resolvedAt: string | null;
  closedAt: string | null;
}

export interface TicketComment {
  id: string;
  body: string;
  isInternal: boolean;
  author: UserBrief;
  createdAt: string;
  editedAt: string | null;
}

export type TicketEventType =
  | "CREATED"
  | "ASSIGNED"
  | "UNASSIGNED"
  | "STATUS_CHANGED"
  | "PRIORITY_CHANGED"
  | "RECLASSIFIED"
  | "COMMENTED"
  | "ATTACHMENT_ADDED"
  | "AI_CLASSIFIED"
  | "SLA_WARNED"
  | "SLA_BREACHED"
  | "RATED"
  | "REOPENED"
  | "AUTO_CLOSED";

export interface TicketEvent {
  id: string;
  eventType: TicketEventType;
  actor: UserBrief | null;
  fieldName: string | null;
  oldValue: string | null;
  newValue: string | null;
  createdAt: string;
}

export interface AllowedTransitions {
  currentStatus: TicketStatus;
  allowedStatuses: TicketStatus[];
  version: number;
}

export interface QueueStats {
  unassigned: number;
  assignedToMe: number;
  inProgress: number;
  atRisk: number;
  breached: number;
}

/* ── Trợ lý ảo (F4) ──────────────────────────────────────────────── */

export interface Citation {
  articleId: string;
  title: string;
  slug: string;
  score: number;
  rank: number;
}

export interface ChatSession {
  id: string;
  title: string | null;
  messageCount: number;
  ledToTicket: boolean;
  createdAt: string;
  lastMessageAt: string | null;
}

export interface ChatMessage {
  id: string;
  role: "USER" | "ASSISTANT" | "SYSTEM";
  content: string;
  noContextFound: boolean;
  citations: Citation[];
  latencyMs: number | null;
  createdAt: string;
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[];
}

/**
 * Sự kiện trong luồng SSE. Backend cam kết thứ tự:
 *   citations → token* → done   |   hoặc error rồi dừng
 */
export type ChatEvent =
  | { type: "citations"; data: { citations: Citation[] } }
  | { type: "token"; data: { delta: string } }
  | {
      type: "done";
      data: {
        messageId: string;
        noContextFound: boolean;
        canCreateTicket?: boolean;
        latencyMs: number;
        promptVersion?: string;
      };
    }
  | {
      type: "error";
      data: { code: string; message: string; canCreateTicket?: boolean };
    };

export interface CreateTicketInput {
  title: string;
  description: string;
  categoryId?: string | null;
  priority?: TicketPriority | null;
}

export interface PaginationMeta {
  page: number;
  pageSize: number;
  totalItems: number;
  totalPages: number;
}

export interface Page<T> {
  data: T[];
  pagination: PaginationMeta;
}

// ─────────────── F3 — AI phân loại & gợi ý người xử lý ───────────────

/** Gợi ý phân loại gần nhất của AI (US-19). `null` khi AI chưa chạy xong. */
export interface AiClassification {
  id: string;
  suggestedCategory: Category | null;
  suggestedPriority: TicketPriority | null;
  confidence: number | null;
  reasoning: string | null;
  wasApplied: boolean;
  wasAccepted: boolean | null;
  modelName: string;
  promptVersion: string;
  latencyMs: number | null;
  errorMessage: string | null;
  createdAt: string;
}

/** Một ứng viên xử lý (US-20). Cố tình KHÔNG có email, như `UserBrief`. */
export interface AssigneeSuggestion {
  agentId: string;
  fullName: string;
  score: number;
  skillLevel: number;
  openTickets: number;
  weightedLoad: number;
  onDuty: boolean;
  reason: string;
}

export interface AssigneeSuggestions {
  category: Category | null;
  suggestions: AssigneeSuggestion[];
  generatedAt: string;
}

/** Định dạng lỗi thống nhất — MỌI lỗi từ backend đều có hình dạng này */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
    requestId: string;
  };
}

/* ── F6 — Thông báo (US-33 → US-36) ─────────────────────────────── */

export type NotificationType =
  | "TICKET_ASSIGNED"
  | "TICKET_STATUS_CHANGED"
  | "TICKET_COMMENTED"
  | "TICKET_RESOLVED"
  | "SLA_AT_RISK"
  | "SLA_BREACHED"
  | "RATING_REQUESTED";

export interface AppNotification {
  id: string;
  type: NotificationType;
  title: string;
  body: string | null;
  /** `entityType` + `entityId` để tự dựng đường dẫn — backend cố tình KHÔNG
   *  trả URL sẵn, vì thông báo cũ sẽ trỏ sai khi frontend đổi route. */
  entityType: string | null;
  entityId: string | null;
  isRead: boolean;
  readAt: string | null;
  createdAt: string;
}

/** Sự kiện đẩy qua kênh WebSocket (ADR-0009) — cùng dữ liệu `AppNotification`
 *  cho `notification:new` để không phải định nghĩa hai kiểu dữ liệu song song
 *  cho cùng một thông báo. */
export type NotificationSocketEvent =
  | { event: "notification:new"; data: AppNotification }
  | { event: "notification:read"; data: { id: string } }
  | { event: "notification:read_all"; data: Record<string, never> };

/* ── US-07 — Quản trị người dùng ────────────────────────────────── */

export interface Department {
  id: string;
  code: string;
  name: string;
}

export interface AdminUser extends UserBrief {
  email: string;
  department: Department | null;
  phone: string | null;
  isActive: boolean;
  lastLoginAt: string | null;
  createdAt: string;
}

export interface CreateUserInput {
  email: string;
  password: string;
  fullName: string;
  role: UserRole;
  departmentId?: string | null;
  phone?: string | null;
}

/* ── F7 — Dashboard (US-37 → US-40) ─────────────────────────────── */

export interface CountBucket {
  key: string;
  label: string;
  count: number;
}

export interface Overview {
  from: string;
  to: string;
  total: number;
  openTotal: number;
  resolvedTotal: number;
  breachedTotal: number;
  unassignedTotal: number;
  /** `null` = chưa có ticket nào, KHÁC với 0 = không vi phạm lần nào. */
  breachRate: number | null;
  byStatus: CountBucket[];
  byPriority: CountBucket[];
  byCategory: CountBucket[];
  daily: CountBucket[];
  generatedAt: string;
  cached: boolean;
}

export interface DurationRow {
  key: string;
  label: string;
  tickets: number;
  firstResponseP50Minutes: number | null;
  firstResponseP90Minutes: number | null;
  resolutionP50Minutes: number | null;
  resolutionP90Minutes: number | null;
}

export interface ResolutionTimeReport {
  from: string;
  to: string;
  rows: DurationRow[];
  generatedAt: string;
}

export interface AgentWorkloadRow {
  agentId: string;
  agentName: string;
  openTickets: number;
  urgentOpen: number;
  resolvedInPeriod: number;
  avgResolutionMinutes: number | null;
  slaBreached: number;
  slaBreachRate: number | null;
  avgRating: number | null;
  ratingCount: number;
}

export interface AgentWorkloadReport {
  from: string;
  to: string;
  rows: AgentWorkloadRow[];
  generatedAt: string;
}

/* ── F5 — Kho tài liệu ──────────────────────────────────────────── */

export interface KbCategory {
  id: string;
  slug: string;
  name: string;
}

export interface ArticleListItem {
  id: string;
  slug: string;
  title: string;
  summary: string | null;
  status: "DRAFT" | "PUBLISHED";
  category: KbCategory | null;
  tags: string[];
  viewCount: number;
  publishedAt: string | null;
  updatedAt: string;
  version: number;
}

export interface ArticleDetail extends ArticleListItem {
  contentMd: string;
  author: { id: string; fullName: string } | null;
}
