import os
import json
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ====== GOOGLE CREDENTIALS ======
credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not credentials_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")

credentials_info = json.loads(credentials_json)

credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)

service = build("calendar", "v3", credentials=credentials)

# ✅ ВАЖНО: ID ТВОЕГО календаря
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
if not CALENDAR_ID:
    raise RuntimeError("GOOGLE_CALENDAR_ID is not set")


def create_booking(name, phone, service, date, time):
    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=1)

    # 🔒 Проверка занятости
    if not is_time_available(start_dt, end_dt):
        return None

    event = {
        "summary": f"{service} — {name}",
        "description": (
            f"Клиент: {name}\n"
            f"Телефон: {phone}\n"
            f"Услуга: {service}"
        ),
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "Europe/Moscow",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Europe/Moscow",
        },
    }

    created_event = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event
    ).execute()

    return created_event.get("htmlLink")



from datetime import timezone

def is_time_available(start_dt, end_dt):
    body = {
        "timeMin": start_dt.replace(tzinfo=timezone.utc).isoformat(),
        "timeMax": end_dt.replace(tzinfo=timezone.utc).isoformat(),
        "items": [{"id": CALENDAR_ID}],
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][CALENDAR_ID]["busy"]

    return len(busy) == 0

