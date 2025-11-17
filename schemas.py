"""
Database Schemas for Booking SaaS (MongoDB + Pydantic)

Each Pydantic model corresponds to a MongoDB collection (lowercased class name).
- Business -> "business"
- Staff -> "staff"
- Service -> "service"
- Availability -> "availability"
- Appointment -> "appointment"
- Reminder -> "reminder"
- Branding -> part of Business document

Notes
- Use UTC timestamps everywhere; convert to/from business timezone in frontend as needed
- Monetary amounts stored in minor units (cents) as integers
- Durations and buffers in minutes
- Timezone stored as IANA TZ string (e.g., "America/New_York")
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Literal

class Branding(BaseModel):
    logo_url: Optional[str] = None
    primary_color: str = "#4f46e5"  # indigo-600
    accent_color: str = "#06b6d4"   # cyan-500
    hero_title: str = "Book your appointment"
    hero_subtitle: str = "Fast, simple scheduling"

class Business(BaseModel):
    owner_id: Optional[str] = Field(None, description="Owner user id if applicable")
    name: str
    slug: str
    timezone: str = "UTC"
    currency: str = "usd"
    ics_token: Optional[str] = None
    deposit_percent_default: int = Field(0, ge=0, le=100)
    reminders_enabled: bool = True
    reminders_email_enabled: bool = True
    reminders_sms_enabled: bool = False
    branding: Branding = Field(default_factory=Branding)

class Staff(BaseModel):
    business_id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    active: bool = True

class Service(BaseModel):
    business_id: str
    name: str
    price_cents: int = Field(..., ge=0)
    duration_min: int = Field(..., ge=5, le=480)
    buffer_before_min: int = Field(0, ge=0, le=120)
    buffer_after_min: int = Field(0, ge=0, le=120)
    deposit_percent_override: Optional[int] = Field(None, ge=0, le=100)

class AvailabilityBlock(BaseModel):
    # One block inside a day (minutes from 00:00)
    start_min: int = Field(..., ge=0, le=24*60)
    end_min: int = Field(..., ge=0, le=24*60)

class Availability(BaseModel):
    business_id: str
    staff_id: str
    # Weekly grid: 0=Mon ... 6=Sun; each day is list of blocks
    weekly: Dict[int, List[AvailabilityBlock]] = Field(default_factory=dict)
    slot_increment_min: int = 15

class Appointment(BaseModel):
    business_id: str
    staff_id: str
    service_id: str
    customer_name: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    # Start and end in ISO 8601 (UTC)
    start_iso: str
    end_iso: str
    status: Literal["pending", "confirmed", "canceled", "completed"] = "pending"
    amount_cents_total: int
    amount_cents_due_now: int
    currency: str = "usd"
    stripe_payment_intent_id: Optional[str] = None
    stripe_checkout_session_id: Optional[str] = None
    notes: Optional[str] = None

class Reminder(BaseModel):
    business_id: str
    appointment_id: str
    kind: Literal["email", "sms"]
    scheduled_at_iso: str
    status: Literal["queued", "sent", "failed"] = "queued"
    last_error: Optional[str] = None
