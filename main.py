import os
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import Business, Staff, Service, Availability, Appointment, Reminder, AvailabilityBlock

# Make Stripe optional so the server can start without the package/keys
try:
    import stripe  # type: ignore
except Exception:
    stripe = None  # fallback

from bson import ObjectId

# Optional imports for email/SMS
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# Config
STRIPE_SECRET = os.getenv("STRIPE_SECRET")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

if stripe and STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

app = FastAPI(title="Booking SaaS API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utility

def oid(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID")


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def compute_deposit_cents(biz: dict, service: dict) -> int:
    pct = service.get("deposit_percent_override")
    if pct is None:
        pct = biz.get("deposit_percent_default", 0)
    return (service["price_cents"] * pct) // 100


def minutes_between(iso_start: str, minutes: int) -> str:
    start = datetime.fromisoformat(iso_start)
    return utc_iso(start + timedelta(minutes=minutes))


# Schemas for requests
class CreateBusiness(BaseModel):
    name: str
    slug: str
    timezone: str = "UTC"
    currency: str = "usd"
    deposit_percent_default: int = 0


class CreateStaff(BaseModel):
    business_id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class CreateService(BaseModel):
    business_id: str
    name: str
    price_cents: int
    duration_min: int
    buffer_before_min: int = 0
    buffer_after_min: int = 0
    deposit_percent_override: Optional[int] = None


class SetAvailability(BaseModel):
    business_id: str
    staff_id: str
    weekly: Dict[int, List[AvailabilityBlock]]
    slot_increment_min: int = 15


class SlotQuery(BaseModel):
    service_id: str
    staff_id: str
    date: str  # YYYY-MM-DD (business timezone assumed)


class BookRequest(BaseModel):
    business_slug: str
    service_id: str
    staff_id: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM (24h)
    customer_name: str
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None


# Routes
@app.get("/")
def root():
    return {"ok": True, "service": "Booking SaaS API"}


@app.get("/test")
def test_database():
    try:
        collections = db.list_collection_names()
        return {
            "backend": "✅ Running",
            "database": "✅ Connected",
            "collections": collections,
        }
    except Exception as e:
        return {"backend": "✅ Running", "database": f"❌ {str(e)}"}


# Business
@app.post("/api/business")
def create_business(payload: CreateBusiness):
    # ensure unique slug
    if db["business"].find_one({"slug": payload.slug}):
        raise HTTPException(status_code=400, detail="Slug already in use")
    biz = Business(
        name=payload.name,
        slug=payload.slug,
        timezone=payload.timezone,
        currency=payload.currency,
        deposit_percent_default=payload.deposit_percent_default,
    )
    _id = create_document("business", biz)
    # Generate private ics token
    token = str(ObjectId())
    db["business"].update_one({"_id": ObjectId(_id)}, {"$set": {"ics_token": token}})
    saved = db["business"].find_one({"_id": ObjectId(_id)})
    saved["_id"] = str(saved["_id"])
    return saved


@app.get("/api/b/{slug}")
def public_business(slug: str):
    biz = db["business"].find_one({"slug": slug})
    if not biz:
        raise HTTPException(404, "Business not found")
    biz["_id"] = str(biz["_id"])
    services = list(db["service"].find({"business_id": str(biz["_id"])}))
    for s in services:
        s["_id"] = str(s["_id"])
    staff = list(db["staff"].find({"business_id": str(biz["_id"]), "active": True}))
    for m in staff:
        m["_id"] = str(m["_id"])
    return {"business": biz, "services": services, "staff": staff}


# Staff
@app.post("/api/staff")
def add_staff(payload: CreateStaff):
    if not db["business"].find_one({"_id": oid(payload.business_id)}):
        raise HTTPException(400, "Business not found")
    staff = Staff(**payload.model_dump())
    _id = create_document("staff", staff)
    doc = db["staff"].find_one({"_id": ObjectId(_id)})
    doc["_id"] = str(doc["_id"])
    return doc


# Service
@app.post("/api/service")
def add_service(payload: CreateService):
    if not db["business"].find_one({"_id": oid(payload.business_id)}):
        raise HTTPException(400, "Business not found")
    service = Service(**payload.model_dump())
    _id = create_document("service", service)
    doc = db["service"].find_one({"_id": ObjectId(_id)})
    doc["_id"] = str(doc["_id"])
    return doc


# Availability
@app.post("/api/availability")
def set_availability(payload: SetAvailability):
    if not db["business"].find_one({"_id": oid(payload.business_id)}):
        raise HTTPException(400, "Business not found")
    if not db["staff"].find_one({"_id": oid(payload.staff_id)}):
        raise HTTPException(400, "Staff not found")
    av = Availability(**payload.model_dump())
    # upsert by business + staff
    db["availability"].update_one(
        {"business_id": av.business_id, "staff_id": av.staff_id},
        {"$set": av.model_dump()},
        upsert=True,
    )
    doc = db["availability"].find_one({"business_id": av.business_id, "staff_id": av.staff_id})
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


# Slot generation helper

def generate_slots(biz: dict, staff_id: str, service: dict, date_str: str, increment: int = 15) -> List[str]:
    # date_str in business timezone is assumed; we treat as local day and produce UTC ISO start strings for slots
    # For simplicity, we consider day boundaries in local time but compute times in UTC as naive offsets
    av = db["availability"].find_one({"business_id": str(biz["_id"]), "staff_id": staff_id})
    if not av:
        return []
    weekday = datetime.fromisoformat(date_str + "T00:00:00+00:00").weekday()  # 0=Mon
    day_blocks = av.get("weekly", {}).get(str(weekday)) or av.get("weekly", {}).get(weekday) or []

    # Fetch existing appointments to block overlaps
    appts = list(db["appointment"].find({
        "business_id": str(biz["_id"]),
        "staff_id": staff_id,
        "status": {"$in": ["pending", "confirmed"]},
        "start_iso": {"$regex": f"^{date_str}"},
    }))

    def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
        return not (a_end <= b_start or a_start >= b_end)

    slots: List[str] = []
    duration_total = service["duration_min"] + service.get("buffer_before_min", 0) + service.get("buffer_after_min", 0)

    # Interpret date at midnight UTC for simplicity
    day_start = datetime.fromisoformat(date_str + "T00:00:00+00:00")

    for block in day_blocks:
        start_min = block.get("start_min") if isinstance(block, dict) else block.start_min
        end_min = block.get("end_min") if isinstance(block, dict) else block.end_min
        t = start_min
        while t + duration_total <= end_min:
            slot_start = day_start + timedelta(minutes=t)
            slot_end = slot_start + timedelta(minutes=duration_total)
            # Check against appointments (respecting service duration + buffers)
            conflict = False
            for ap in appts:
                ap_s = datetime.fromisoformat(ap["start_iso"])  # stored in UTC ISO
                ap_e = datetime.fromisoformat(ap["end_iso"])  # includes buffers already when created
                if overlaps(slot_start, slot_end, ap_s, ap_e):
                    conflict = True
                    break
            if not conflict:
                slots.append(utc_iso(slot_start))
            t += increment
    return slots


@app.post("/api/b/{slug}/slots")
def slots(slug: str, q: SlotQuery):
    biz = db["business"].find_one({"slug": slug})
    if not biz:
        raise HTTPException(404, "Business not found")
    service = db["service"].find_one({"_id": oid(q.service_id)})
    if not service:
        raise HTTPException(404, "Service not found")
    av = db["availability"].find_one({"business_id": str(biz["_id"]), "staff_id": q.staff_id})
    increment = av.get("slot_increment_min", 15) if av else 15
    times = generate_slots(biz, q.staff_id, service, q.date, increment)
    # Return times as HH:MM based on provided date
    formatted = [datetime.fromisoformat(t).strftime("%H:%M") for t in times]
    return {"date": q.date, "times": formatted}


@app.post("/api/b/{slug}/book")
def book(slug: str, payload: BookRequest):
    biz = db["business"].find_one({"slug": slug})
    if not biz:
        raise HTTPException(404, "Business not found")
    service = db["service"].find_one({"_id": oid(payload.service_id)})
    staff = db["staff"].find_one({"_id": oid(payload.staff_id)})
    if not service or not staff:
        raise HTTPException(400, "Invalid staff or service")

    # Build start time in UTC using date + time
    start_iso = payload.date + "T" + payload.time + ":00+00:00"
    duration_total = service["duration_min"] + service.get("buffer_before_min", 0) + service.get("buffer_after_min", 0)
    end_iso = minutes_between(start_iso, duration_total)

    # Check slot availability again
    available = slots(slug, SlotQuery(service_id=payload.service_id, staff_id=payload.staff_id, date=payload.date))
    if payload.time not in available["times"]:
        raise HTTPException(409, detail="Selected time is no longer available")

    amount_total = service["price_cents"]
    amount_due = compute_deposit_cents(biz, service)
    if amount_due <= 0:
        amount_due = amount_total

    appt = Appointment(
        business_id=str(biz["_id"]),
        staff_id=payload.staff_id,
        service_id=payload.service_id,
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        customer_phone=payload.customer_phone,
        start_iso=start_iso,
        end_iso=end_iso,
        status="pending",
        amount_cents_total=amount_total,
        amount_cents_due_now=amount_due,
        currency=biz.get("currency", "usd"),
    )
    appt_id = create_document("appointment", appt)

    # Create Stripe Checkout Session for payment (only if stripe lib and key available)
    checkout_url = None
    if stripe and STRIPE_SECRET:
        success_url = f"{FRONTEND_URL}/payment-success?appointment_id={appt_id}"
        cancel_url = f"{FRONTEND_URL}/payment-cancel?appointment_id={appt_id}"
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                payment_intent_data={
                    "metadata": {
                        "appointment_id": appt_id,
                        "business_id": str(biz["_id"]),
                    }
                },
                line_items=[{
                    "price_data": {
                        "currency": appt.currency,
                        "product_data": {
                            "name": service["name"],
                            "metadata": {"appointment_id": appt_id},
                        },
                        "unit_amount": amount_due,
                    },
                    "quantity": 1,
                }],
                success_url=success_url,
                cancel_url=cancel_url,
            )
            checkout_url = session.url
            db["appointment"].update_one({"_id": ObjectId(appt_id)}, {"$set": {"stripe_checkout_session_id": session.id}})
        except Exception as e:
            # leave without payment link if Stripe not configured correctly
            print("Stripe error:", e)

    return {"appointment_id": appt_id, "checkout_url": checkout_url}


# Stripe webhook to auto-confirm appointments
@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    # If stripe lib or secret missing, accept webhook without processing to avoid startup/runtime failure
    if not (stripe and STRIPE_WEBHOOK_SECRET):
        return {"received": True}
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))

    if event["type"] in ("payment_intent.succeeded", "checkout.session.completed"):
        data = event["data"]["object"]
        appt_id = None
        if event["type"] == "payment_intent.succeeded":
            appt_id = data.get("metadata", {}).get("appointment_id")
        else:
            appt_id = data.get("metadata", {}).get("appointment_id") or data.get("payment_intent")
        if appt_id:
            db["appointment"].update_one({"_id": oid(appt_id)}, {"$set": {"status": "confirmed"}})
    return {"received": True}


# ICS feed
@app.get("/api/b/{slug}/ics", response_class=PlainTextResponse)
def ics_feed(slug: str, token: str):
    biz = db["business"].find_one({"slug": slug})
    if not biz:
        raise HTTPException(404, "Business not found")
    if token != biz.get("ics_token"):
        raise HTTPException(403, "Invalid token")
    # Basic ICS content
    appts = list(db["appointment"].find({
        "business_id": str(biz["_id"]),
        "status": {"$in": ["confirmed", "completed"]},
    }))
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//BookingSaaS//EN"]
    for ap in appts:
        start = datetime.fromisoformat(ap["start_iso"]).strftime("%Y%m%dT%H%M%SZ")
        end = datetime.fromisoformat(ap["end_iso"]).strftime("%Y%m%dT%H%M%SZ")
        uid = str(ap["_id"]) + "@bookingsaas"
        summary = "Appointment"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{summary}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# Dashboard: list appointments
@app.get("/api/appointments")
def list_appointments(business_id: str, status: Optional[str] = None, limit: int = 200):
    q: Dict = {"business_id": business_id}
    if status:
        q["status"] = status
    appts = list(db["appointment"].find(q).sort("start_iso", 1).limit(limit))
    for a in appts:
        a["_id"] = str(a["_id"])
    return {"items": appts}


@app.get("/api/appointments/export")
def export_appointments(business_id: str):
    appts = list(db["appointment"].find({"business_id": business_id}).sort("start_iso", 1))
    def generate():
        header = [
            "id","status","start_iso","end_iso","customer_name","customer_email","customer_phone","service_id","staff_id","amount_cents_total","amount_cents_due_now","currency"
        ]
        yield ",".join(header) + "\n"
        for a in appts:
            row = [
                str(a.get("_id")), a.get("status",""), a.get("start_iso",""), a.get("end_iso",""),
                a.get("customer_name",""), a.get("customer_email",""), a.get("customer_phone",""),
                a.get("service_id",""), a.get("staff_id",""), str(a.get("amount_cents_total",0)), str(a.get("amount_cents_due_now",0)), a.get("currency","usd")
            ]
            yield ",".join([str(x).replace(","," ") for x in row]) + "\n"
    return StreamingResponse(generate(), media_type="text/csv")


# Reminder cron
@app.post("/api/cron/reminders")
def run_reminders():
    now = datetime.now(timezone.utc)
    # Find confirmed appointments starting at 24h or 2h from now (tolerance 5 minutes)
    for hours in (24, 2):
        target_start = now + timedelta(hours=hours)
        start_window = target_start - timedelta(minutes=5)
        end_window = target_start + timedelta(minutes=5)
        appts = list(db["appointment"].find({
            "status": "confirmed",
            "start_iso": {"$gte": utc_iso(start_window), "$lte": utc_iso(end_window)}
        }))
        for ap in appts:
            biz = db["business"].find_one({"_id": oid(ap["business_id"])})
            if not biz or not biz.get("reminders_enabled", True):
                continue
            if biz.get("reminders_email_enabled", True):
                create_document("reminder", Reminder(
                    business_id=ap["business_id"], appointment_id=str(ap["_id"]), kind="email",
                    scheduled_at_iso=utc_iso(now)
                ))
            if biz.get("reminders_sms_enabled", False):
                create_document("reminder", Reminder(
                    business_id=ap["business_id"], appointment_id=str(ap["_id"]), kind="sms",
                    scheduled_at_iso=utc_iso(now)
                ))
    return {"queued": True}


@app.post("/api/reminders/send")
def send_reminders():
    # send queued reminders; integrate with Resend and Twilio if env vars set
    resend_key = os.getenv("RESEND_API_KEY")
    tw_sid = os.getenv("TWILIO_ACCOUNT_SID")
    tw_token = os.getenv("TWILIO_AUTH_TOKEN")
    tw_from = os.getenv("TWILIO_FROM_NUMBER")

    queued = list(db["reminder"].find({"status": "queued"}).limit(50))
    sent = 0
    failed = 0

    for r in queued:
        try:
            ap = db["appointment"].find_one({"_id": r["appointment_id"] if isinstance(r["appointment_id"], ObjectId) else oid(r["appointment_id"])})
            if not ap:
                db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "failed", "last_error": "Appointment not found"}})
                failed += 1
                continue
            # Email
            if r["kind"] == "email" and resend_key and ap.get("customer_email"):
                import requests
                resp = requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={
                        "from": "Bookings <noreply@bookingsaas.dev>",
                        "to": [ap["customer_email"]],
                        "subject": "Appointment reminder",
                        "html": f"<p>Hi {ap.get('customer_name','')}, this is a reminder for your appointment at {ap.get('start_iso')}</p>",
                    },
                    timeout=10,
                )
                if resp.status_code >= 200 and resp.status_code < 300:
                    db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "sent"}})
                    sent += 1
                else:
                    db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "failed", "last_error": resp.text}})
                    failed += 1
            elif r["kind"] == "sms" and tw_sid and tw_token and tw_from and ap.get("customer_phone") and TwilioClient:
                client = TwilioClient(tw_sid, tw_token)
                msg = client.messages.create(
                    body=f"Reminder: appointment at {ap.get('start_iso')}",
                    from_=tw_from,
                    to=ap["customer_phone"],
                )
                db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "sent"}})
                sent += 1
            else:
                # No integration keys, mark sent to avoid infinite queue in demo
                db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "sent", "last_error": "No provider configured; marked sent in demo"}})
                sent += 1
        except Exception as e:
            db["reminder"].update_one({"_id": r["_id"]}, {"$set": {"status": "failed", "last_error": str(e)}})
            failed += 1
    return {"sent": sent, "failed": failed}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
