from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta

SCOPES = ["https://www.googleapis.com/auth/calendar"]
SERVICE_ACCOUNT_FILE = "credentials.json"
CALENDAR_ID = "primary"  # или конкретный календарь

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

service = build("calendar", "v3", credentials=credentials)


def create_booking(name, phone, service_name, date, time):
    start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end = start + timedelta(hours=1)

    event = {
        "summary": f"{service_name} — {name}",
        "description": f"Телефон: {phone}",
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Europe/Berlin",
        },
    }

    created_event = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event
    ).execute()

    return created_event.get("htmlLink")
