import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build


# ====== CONFIG ======
TZ_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
if not CALENDAR_ID:
    raise RuntimeError("GOOGLE_CALENDAR_ID is not set")

credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not credentials_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")

credentials_info = json.loads(credentials_json)

credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/calendar"],
)

service = build("calendar", "v3", credentials=credentials)


def _local_dt(date_str: str, time_str: str) -> datetime:
    # date_str: YYYY-MM-DD, time_str: HH:MM
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TZ)


def _to_utc_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def check_slot_available(date_str: str, time_str: str, duration_minutes: int = 60) -> bool:
    start_local = _local_dt(date_str, time_str)
    end_local = start_local + timedelta(minutes=duration_minutes)
    return is_time_available(start_local, end_local)


def is_time_available(start_local: datetime, end_local: datetime) -> bool:
    body = {
        "timeMin": _to_utc_rfc3339(start_local),
        "timeMax": _to_utc_rfc3339(end_local),
        "items": [{"id": CALENDAR_ID}],
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][CALENDAR_ID]["busy"]
    return len(busy) == 0


def create_booking(
    name: str,
    phone: str,
    service_name: str,
    date_str: str,
    time_str: str,
    duration_minutes: int = 60,
):
    start_local = _local_dt(date_str, time_str)
    end_local = start_local + timedelta(minutes=duration_minutes)

    # 🔒 Проверка занятости (ещё раз)
    if not is_time_available(start_local, end_local):
        return None

    event = {
        "summary": f"{service_name} — {name}",
        "description": (
            f"Клиент: {name}\n"
            f"Телефон: {phone}\n"
            f"Услуга: {service_name}"
        ),
        "start": {
            "dateTime": start_local.isoformat(),
            "timeZone": TZ_NAME,
        },
        "end": {
            "dateTime": end_local.isoformat(),
            "timeZone": TZ_NAME,
        },
    }

    created_event = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
    ).execute()

    return created_event.get("htmlLink")
