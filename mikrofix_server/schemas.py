"""HTTP schemas for version 1 of the synchronization boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    operation_type: str = Field(min_length=1, max_length=100)
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: UUID
    expected_version: int = Field(ge=0)
    actor_id: str = Field(min_length=1, max_length=200)
    actor_user_id: UUID | None = None
    actor_authorization_version: int = Field(default=0, ge=0)
    device_id: str = Field(min_length=1, max_length=200)
    device_sequence: int = Field(ge=1)
    occurred_at: datetime
    payload: dict[str, Any]
    contract_version: int
    workstation_signature: str = Field(default="", max_length=200)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_has_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value


class OperationReceipt(BaseModel):
    operation_id: UUID
    result_code: str
    aggregate_id: UUID
    aggregate_version: int | None = None
    central_sequence: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class ChangeItem(BaseModel):
    central_sequence: int
    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    occurred_at: datetime
    payload: dict[str, Any]


class ChangePage(BaseModel):
    after: int
    next_checkpoint: int
    changes: list[ChangeItem]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    pin: str = Field(min_length=6, max_length=14, pattern=r"^[0-9]+$")
    workstation_code: str = Field(min_length=1, max_length=100)
    workstation_secret: str = Field(min_length=32, max_length=500)
    application_type: str = Field(pattern=r"^(SERVICE|CASHIER|OFFICE)$")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user_id: UUID
    user_version: int
    username: str
    display_name: str
    role: str
    workstation_code: str
    application_type: str
    permissions: list[str]
    must_change_pin: bool


class CreateCentralUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern=r"^(ADMIN|SERVICE|OFFICE|BUYBACK|CASHIER)$")
    pin: str = Field(min_length=6, max_length=14, pattern=r"^[0-9]+$")
    permissions: list[str] | None = None
    operator_tax_number: str = Field(default="", pattern=r"^$|^[0-9]{8}$")
    must_change_pin: bool = True


class RegisterWorkstationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workstation_code: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    application_type: str = Field(pattern=r"^(SERVICE|CASHIER|OFFICE)$")
    signing_public_key: str = ""


class UpdateCentralUserAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern=r"^(ADMIN|SERVICE|OFFICE|BUYBACK|CASHIER)$")
    permissions: list[str]
    is_active: bool
    operator_tax_number: str = Field(default="", pattern=r"^$|^[0-9]{8}$")
    expected_version: int = Field(ge=1)


class SetWorkstationActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


class ChangeOwnPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_pin: str = Field(min_length=6, max_length=14, pattern=r"^[0-9]+$")
    new_pin: str = Field(min_length=6, max_length=14, pattern=r"^[0-9]+$")
