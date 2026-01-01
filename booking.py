import os
import json
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

CALENDAR_ID = "airsmashеr@gmail.com"

# ====== GOOGLE CREDENTIALS ======
credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not credentials_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")

# ВАЖНО: json.loads — ТОЛЬКО ОДИН РАЗ
credentials_info = json.loads(credentials_json)

credentials = service_account.Credentials.from_service_account_info(
    credentials_info,
    scopes=["https://www.googleapis.com/auth/calendar"]
)

service = build("calendar", "v3", credentials=credentials)

# ID календаря (обычно email сервисного аккаунта или 'primary')
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")


# ====== CREATE BOOKING ======
def create_booking(name, phone, service_name, date, time):
    """
    Создаёт событие в Google Calendar
    """

    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=1)

    event = {
        "summary": f"💆 Тайский массаж — {name}",
        "description": (
            f"Имя: {name}\n"
            f"Телефон: {phone}\n"
            f"Услуга: {service_name}"
        ),
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "Europe/Berlin",
        },
    }

    created_event = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event
    ).execute()

    return created_event.get("htmlLink")
