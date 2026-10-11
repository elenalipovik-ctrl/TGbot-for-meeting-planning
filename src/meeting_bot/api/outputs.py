"""Response DTOs exported to OpenAPI; only allowlisted public fields."""
from typing import Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict

T=TypeVar("T")

class Envelope(BaseModel, Generic[T]):
    data: T
    server_time: int
    request_id: str
    next_cursor: str|None=None

class Output(BaseModel):
    model_config=ConfigDict(extra="forbid")
    operation_id: str|None=None
    operation_state: Literal["done","accepted"]|None=None

class SessionData(BaseModel):
    bearer: str
    expires_at: int

class BootstrapData(Output):
    telegram_id: str
    role: Literal["admin","visitor"]
    access_status: Literal["new","pending","allowed","rejected","blocked"]
    username: str|None
    policy_version: str
    policy_url: str
    time_zone: str
    durations: dict[str,list[int]]
    capabilities: dict[str,bool]
    allowed_actions: list[str]

class TemplateData(Output):
    id: int
    name: str
    description: str
    instructions: str
    is_active: bool
    revision: int

class ProfileData(Output):
    name: str|None
    email: str|None
    phone: str|None
    username: str|None
    revision: int

class DraftFields(BaseModel):
    step: str
    template_id: int|None
    duration_minutes: int|None
    video_service: str|None
    selected_day: str|None
    booking_id: int|None
    contact_name: str|None
    contact_email: str|None
    contact_phone: str|None
    purpose: str|None
    expires_at: int
    used_saved_contacts: bool
    hold_expires_at: int|None

class DraftData(Output):
    revision: int
    draft: DraftFields|None

class TemplateSnapshot(BaseModel):
    name: str|None
    description: str|None
    instructions: str|None

class ContactsSnapshot(BaseModel):
    name: str|None
    username: str|None
    email: str|None
    phone: str|None

class BookingData(Output):
    id: int
    status: str
    starts_at: int
    ends_at: int
    time_zone: str
    duration_minutes: int
    video_service: str|None
    template: TemplateSnapshot
    contacts: ContactsSnapshot
    purpose: str|None
    hold_expires_at: int|None
    decision_deadline_at: int|None
    revision: int
    cancel_until: int|None
    cancellation_pending: bool
    video_url: str|None
    sync_status: str|None
    allowed_actions: list[str]

class BookingSummary(BaseModel):
    id: int
    starts_at: int
    status: str
    revision: int
    ends_at: int
    time_zone: str
    template_name: str|None
    video_service: str|None
    decision_deadline_at: int|None

class HoldData(Output):
    booking: BookingData
    current: DraftData

class SlotData(BaseModel):
    starts_at: int
    ends_at: int
    interval_id: int
    time_zone: str

class SlotsData(BaseModel):
    slots: list[SlotData]
    checked_at: int

class VisitorData(Output):
    telegram_id: str
    username: str|None
    access_status: str
    revision: int

class AccessRequestData(BaseModel):
    id: int
    telegram_id: str
    username: str|None
    requested_at: int
    revision: int

class IntervalData(BaseModel):
    id: int
    starts_at: int
    ends_at: int
    time_zone: str
    is_published: bool
    manual_exception: bool
    revision: int

class AvailabilityResult(Output):
    interval_id: int|None
    booking_ids: list[int]
    preview: bool

class ImpactData(BaseModel):
    booking_ids: list[int]
    count: int
    preview: bool

class BlockData(BaseModel):
    id: int
    starts_at: int
    ends_at: int
    revision: int

class BlockResult(Output):
    block_id: int
    booking_ids: list[int]
    released: bool

class WindowData(BaseModel):
    weekday: int
    starts_minute: int
    ends_minute: int

class ScheduleData(Output):
    time_zone: str
    revision: int
    windows: list[WindowData]

class GoogleData(Output):
    status: Literal["not_checked","working","reconnect_required","permission_denied","unavailable"]
    checked_at: int|None
    error_code: str|None
    stale: bool

class AlertsData(BaseModel):
    calendar_attention: int
    notification_failures: int
    claimed_jobs: int
    awaiting_link: int
    notification_booking_ids: list[int]

class StatisticsData(BaseModel):
    access: int
    total: int
    confirmed: int
    rejected: int
    expired: int
    cancelled: int
    repeat: int
    reused: int
    telemost: int
    zoom: int
    average_minutes: int

class LogoutData(BaseModel):
    revoked: bool


class StartOptionsData(Output):
    revision: int
    minutes: list[int]

class SchedulePreviewData(BaseModel):
    submitted_at: int
    decision_deadline_at: int
