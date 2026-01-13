# booking.py
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Moscow"))

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID") or os.getenv("CALENDAR_ID")
CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not CALENDAR_ID:
    raise RuntimeError("GOOGLE_CALENDAR_ID not set")
if not CREDS_JSON:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("booking")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _service():
    info = json.loads(CREDS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def _local_dt(date_str: str, time_str: str) -> datetime:
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TZ)

def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()

def _list_busy(time_min: datetime, time_max: datetime) -> List[Tuple[datetime, datetime]]:
    svc = _service()
    body = {
        "timeMin": _to_rfc3339(time_min),
        "timeMax": _to_rfc3339(time_max),
        "items": [{"id": CALENDAR_ID}],
    }
    resp = svc.freebusy().query(body=body).execute()
    busy = resp.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    out = []
    for b in busy:
        out.append((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])))
    return out

def check_slot_available(date_str: str, time_str: str, duration_minutes: int) -> bool:
    start = _local_dt(date_str, time_str)
    end = start + timedelta(minutes=duration_minutes)
    try:
        busy = _list_busy(start - timedelta(minutes=1), end + timedelta(minutes=1))
    except Exception as e:
        logger.warning("freebusy failed: %s", e)
        # safer default: don't allow if we can't check
        return False
    for bs, be in busy:
        # normalize tz
        bs = bs.astimezone(TZ)
        be = be.astimezone(TZ)
        if start < be and end > bs:
            return False
    return True

def suggest_next_slots(duration_minutes: int, limit: int = 5, days_ahead: int = 14, slot_minutes: int = 30) -> List[Tuple[str, str]]:
    now = datetime.now(TZ) + timedelta(minutes=5)
    start_day = now.replace(second=0, microsecond=0)

    results: List[Tuple[str, str]] = []
    for day_offset in range(days_ahead + 1):
        day = (start_day + timedelta(days=day_offset)).date()
        # working hours (customize)
        work_start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ)
        work_end = datetime(day.year, day.month, day.day, 21, 0, tzinfo=TZ)

        cursor = max(work_start, start_day) if day_offset == 0 else work_start
        while cursor + timedelta(minutes=duration_minutes) <= work_end:
            d = cursor.date().isoformat()
            t = cursor.strftime("%H:%M")
            if check_slot_available(d, t, duration_minutes):
                results.append((d, t))
                if len(results) >= limit:
                    return results
            cursor += timedelta(minutes=slot_minutes)
    return results

def create_booking(
    date_str: str,
    time_str: str,
    service_name: str,
    client_name: str,
    phone: str,
    duration_minutes: int = 60,
    comment: str = "",
) -> Optional[str]:
    svc = _service()
    start = _local_dt(date_str, time_str)
    end = start + timedelta(minutes=duration_minutes)

    summary = f"{service_name} — {client_name}"
    description = f"Телефон: {phone}\n"
    if comment:
        description += f"Комментарий: {comment}\n"

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": _to_rfc3339(start), "timeZone": str(TZ)},
        "end": {"dateTime": _to_rfc3339(end), "timeZone": str(TZ)},
    }

    created = svc.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    html_link = created.get("htmlLink")
    return html_link
