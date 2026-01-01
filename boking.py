import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_calendar_service():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set")

    creds_dict = json.loads(creds_json)

    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )

    return build("calendar", "v3", credentials=credentials)

# 🔹 создать событие
def create_booking(summary, description, start_iso, end_iso):
    service = get_calendar_service()

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Europe/Berlin"},
        "end": {"dateTime": end_iso, "timeZone": "Europe/Berlin"},
    }

    return service.events().insert(
        calendarId="primary",
        body=event
    ).execute()
