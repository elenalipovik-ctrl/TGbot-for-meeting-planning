"""Strict request DTOs. Identity, status, clocks and secrets are never client fields."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..validation import valid_email, valid_phone

Revision = Annotated[int, Field(strict=True, ge=0)]
ObjectId = Annotated[int, Field(strict=True, gt=0)]
Timestamp = Annotated[int, Field(strict=True, ge=0, le=4102444800)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=500)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def literal_types(cls, value, info):
        if info.field_name in ("confirmed", "accepted") and type(value) is not bool:
            raise ValueError("boolean")
        if info.field_name == "duration_minutes" and value is not None and type(value) is not int:
            raise ValueError("integer")
        return value


class SessionInput(Input):
    # Raw query bytes must not be trimmed or otherwise reserialized before HMAC.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    init_data: Annotated[str, Field(strict=True, min_length=1, max_length=16384)]


class VersionInput(Input):
    revision: Revision


class ConfirmInput(VersionInput):
    confirmed: Literal[True]


class AccessRequestInput(Input):
    policy_version: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    accepted: Literal[True]


class ConsentInput(VersionInput):
    policy_version: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    action: Literal["accepted", "withdrawn"]


class HoldInput(VersionInput):
    template_id: ObjectId
    interval_id: ObjectId
    starts_at: Timestamp
    duration_minutes: Literal[15, 30, 45, 60, 90]
    video_service: Literal["telemost", "zoom"]

    @model_validator(mode="after")
    def allowed_duration(self):
        if self.video_service == "zoom" and self.duration_minutes not in (15, 30):
            raise ValueError("duration")
        return self


class DraftInput(VersionInput):
    contact_name: Text | None = None
    contact_email: Text | None = None
    contact_phone: Annotated[str, Field(strict=True, max_length=25)] | None = None
    purpose: Text | None = None

    @model_validator(mode="after")
    def contacts(self):
        changed = self.model_fields_set - {"revision"}
        if not changed:
            raise ValueError("empty patch")
        for key in changed:
            value = getattr(self, key)
            if value is None and key != "contact_phone":
                raise ValueError("required text")
        if "contact_email" in changed and not valid_email(self.contact_email):
            raise ValueError("email")
        if self.contact_phone and not valid_phone(self.contact_phone):
            raise ValueError("phone")
        return self


class SubmitInput(VersionInput):
    policy_version: Annotated[str, Field(strict=True, min_length=1, max_length=100)]


class ProfileInput(VersionInput):
    name: Text
    email: Text
    phone: Annotated[str, Field(strict=True, max_length=25)] | None = None

    @model_validator(mode="after")
    def contacts(self):
        if not valid_email(self.email) or (self.phone and not valid_phone(self.phone)):
            raise ValueError("contacts")
        return self


class AccessDecisionInput(VersionInput):
    status: Literal["allowed", "rejected", "blocked"]


class DecisionInput(VersionInput):
    approve: Annotated[bool, Field(strict=True)]


class VideoInput(VersionInput):
    video_url: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]


class AvailabilityInput(Input):
    starts_at: Timestamp
    ends_at: Timestamp
    time_zone: Literal["Europe/Moscow"]
    manual_exception: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def ordered(self):
        if self.starts_at >= self.ends_at:
            raise ValueError("interval")
        return self


class BlockInput(Input):
    starts_at: Timestamp
    ends_at: Timestamp
    confirmed: Literal[True]

    @model_validator(mode="after")
    def ordered(self):
        if self.starts_at >= self.ends_at:
            raise ValueError("interval")
        return self


class DayInput(Input):
    local_date: date
    time_zone: Literal["Europe/Moscow"]
    confirmed: Literal[True]


class ImpactInput(Input):
    kind: Literal["interval", "day", "range"]
    interval_id: ObjectId | None = None
    local_date: date | None = None
    starts_at: Timestamp | None = None
    ends_at: Timestamp | None = None

    @model_validator(mode="after")
    def target(self):
        if self.kind == "interval" and (self.interval_id is None or self.local_date is not None or self.starts_at is not None or self.ends_at is not None):
            raise ValueError("target")
        if self.kind == "day" and (self.local_date is None or self.interval_id is not None or self.starts_at is not None or self.ends_at is not None):
            raise ValueError("target")
        if self.kind == "range" and (self.starts_at is None or self.ends_at is None or self.starts_at >= self.ends_at or self.interval_id is not None or self.local_date is not None):
            raise ValueError("target")
        return self


class TemplateInput(Input):
    name: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    description: Annotated[str, Field(strict=True, max_length=500)]
    instructions: Annotated[str, Field(strict=True, max_length=500)]


class TemplateEditInput(TemplateInput):
    revision: Revision
    is_active: Annotated[bool, Field(strict=True)]


class ReviewWindowInput(Input):
    weekday: Annotated[int, Field(strict=True, ge=0, le=6)]
    starts_minute: Annotated[int, Field(strict=True, ge=0, le=1439)]
    ends_minute: Annotated[int, Field(strict=True, ge=1, le=1440)]


class ScheduleInput(VersionInput):
    time_zone: Literal["Europe/Moscow"]
    windows: Annotated[list[ReviewWindowInput], Field(min_length=1, max_length=70)]


class ErrorData(BaseModel):
    code: str
    message: str
    retryable: bool = False
    field_errors: dict[str, str] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorData
    request_id: str
    server_time: int


class SuccessResponse(BaseModel):
    data: dict | list | None
    server_time: int
    request_id: str
    next_cursor: str | None = None


class SelectionInput(VersionInput):
    template_id: ObjectId | None = None
    duration_minutes: Literal[15,30,45,60,90] | None = None
    video_service: Literal["telemost","zoom"] | None = None
    selected_day: date | None = None


class StartOptionsInput(VersionInput):
    minutes: Annotated[list[Annotated[int,Field(strict=True,ge=0,le=1439,multiple_of=15)]],Field(max_length=96)]
