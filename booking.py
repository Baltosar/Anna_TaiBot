import os
import json
import re
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


# ====== TIME HELPERS ======
def now_local() -> datetime:
    return datetime.now(TZ)


def _local_dt(date_str: str, time_str: str) -> datetime:
    # date_str: YYYY-MM-DD, time_str: HH:MM
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=TZ)


def _to_utc_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def is_future_slot(start_local: datetime) -> bool:
    # строго в будущем (если уже наступило — нельзя)
    return start_local > now_local()


# ====== PARSER (для "сегодня 10:00" и т.п.) ======
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b")
_TIME_HM = re.compile(r"\b([01]?\d|2[0-3])[:.](\d{2})\b")


def parse_datetime_from_text(text: str) -> tuple[str | None, str | None]:
    """
    Пытаемся вытащить date_str(YYYY-MM-DD) и time_str(HH:MM) из текста:
    - "сегодня 10:00"
    - "завтра 18:30"
    - "05.01 10:00" (год берём текущий)
    - "2026-01-15 10:00"
    """
    t = (text or "").lower().strip()
    tm = _TIME_HM.search(t)
    if not tm:
        return None, None

    hh, mm = tm.group(1), tm.group(2)
    time_str = f"{int(hh):02d}:{int(mm):02d}"
    time_span = tm.span()

    today = now_local().date()

    if "сегодня" in t:
        return today.strftime("%Y-%m-%d"), time_str

    if "завтра" in t:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d"), time_str

    # ISO YYYY-MM-DD
    iso = _DATE_ISO.search(t)
    if iso:
        y, m, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        try:
            datetime(y, m, d)  # валидация
            return f"{y:04d}-{m:02d}-{d:02d}", time_str
        except ValueError:
            # если вдруг плохая дата — игнорируем
            pass

    # DMY: ищем ТОЛЬКО такие совпадения, которые НЕ перекрывают найденное время,
    # иначе "18.30" (время) будет ошибочно считаться датой.
    for mobj in _DATE_DMY.finditer(t):
        s, e = mobj.span()
        # перекрывается со временем? пропускаем
        if not (e <= time_span[0] or s >= time_span[1]):
            continue

        d = int(mobj.group(1))
        m = int(mobj.group(2))
        y_raw = mobj.group(3)

        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            y = today.year

        try:
            datetime(y, m, d)  # валидация
            return f"{y:04d}-{m:02d}-{d:02d}", time_str
        except ValueError:
            # например 2026-30-18 — просто пропустим и попробуем следующее совпадение
            continue

    # время есть, даты нет — вернём только время
    return None, time_str



# ====== AVAILABILITY ======
def check_slot_available(date_str: str, time_str: str, duration_minutes: int = 60) -> bool:
    try:
        start_local = _local_dt(date_str, time_str)
    except ValueError:
        return False

    if not is_future_slot(start_local):
        return False
    end_local = start_local + timedelta(minutes=duration_minutes)
    return is_time_available(start_local, end_local)


def is_time_available(start_local: datetime, end_local: datetime) -> bool:
    # Доп. защита: не ходим в Google за прошлым
    if not is_future_slot(start_local):
        return False

    body = {
        "timeMin": _to_utc_rfc3339(start_local),
        "timeMax": _to_utc_rfc3339(end_local),
        "items": [{"id": CALENDAR_ID}],
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][CALENDAR_ID]["busy"]
    return len(busy) == 0


def suggest_next_free_slots(
    start_from_local: datetime | None = None,
    duration_minutes: int = 60,
    step_minutes: int = 30,
    limit: int = 5,
    search_hours: int = 72,
) -> list[tuple[str, str]]:
    """
    Ищем ближайшие свободные слоты, начиная с момента start_from_local (или now).
    Возвращаем список (date_str, time_str).
    """
    if start_from_local is None:
        start_from_local = now_local()

    # стартуем с ближайшего шага (например 18:07 -> 18:30)
    minute = start_from_local.minute
    add = (step_minutes - (minute % step_minutes)) % step_minutes
    cursor = start_from_local.replace(second=0, microsecond=0) + timedelta(minutes=add)

    found: list[tuple[str, str]] = []
    end_search = cursor + timedelta(hours=search_hours)

    while cursor < end_search and len(found) < limit:
        date_str = cursor.strftime("%Y-%m-%d")
        time_str = cursor.strftime("%H:%M")
        if check_slot_available(date_str, time_str, duration_minutes=duration_minutes):
            found.append((date_str, time_str))
        cursor += timedelta(minutes=step_minutes)

    return found


# ====== CREATE BOOKING ======
def create_booking(
    name: str,
    phone: str,
    service_name: str,
    date_str: str,
    time_str: str,
    duration_minutes: int = 60,
):
    try:
        start_local = _local_dt(date_str, time_str)
    except ValueError:
        return None


    # 🔒 Запрещаем прошлое
    if not is_future_slot(start_local):
        return None

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

