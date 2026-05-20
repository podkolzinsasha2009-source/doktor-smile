from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import asyncio
import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz
import re
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Константы ───────────────────────────────────────────────────────────────

MOSCOW_TZ        = pytz.timezone("Europe/Moscow")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "вкстомат тест")
CREDENTIALS_FILE = "google_credentials.json"
WORK_START       = 8
WORK_END         = 20
DAYS_AHEAD       = 14
CANCEL_HOURS     = 24

OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TG_CHAT_ID", "")

CLINIC_CONTEXT = """Ты ассистент стоматологической клиники «Доктор Смайл».
Адрес: ул. Профсоюзная, 87, Москва. Режим работы: ежедневно 8:00–20:00.
Телефон: +7 (495) 123-45-67.
Врачи: Воронов Дмитрий Александрович (ортопед), Новиков Павел Игоревич (ортопед),
Соколова Екатерина Дмитриевна (гигиенист).
Цены: консультация от 500 руб, кариес от 3500 руб, удаление от 1500 руб,
чистка от 3000 руб, брекеты металл 45000 руб, керамика 65000 руб.
Отвечай коротко, по делу, на русском. Если вопрос не о клинике — мягко
переведи на тему стоматологии."""

DOCTORS = [
    {"id": 1, "name": "Воронов Дмитрий Александрович", "spec": "Стоматолог-ортопед", "initials": "ВД"},
    {"id": 2, "name": "Новиков Павел Игоревич",        "spec": "Стоматолог-ортопед", "initials": "НП"},
    {"id": 3, "name": "Соколова Екатерина Дмитриевна", "spec": "Гигиенист",          "initials": "СЕ"},
]


# ── Telegram ─────────────────────────────────────────────────────────────────

async def tg_send(text: str):
    """Асинхронная отправка сообщения в Telegram."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        print(f"[TG ERROR] {e}")


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    # Читаем credentials из переменной окружения (Render) или из файла (локально)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds).open(SPREADSHEET_NAME).sheet1


def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def parse_dt_str(dt_str: str):
    dt_str = dt_str.strip().lstrip("'").strip()
    if not dt_str:
        return None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


def can_modify(dt_str: str) -> bool:
    dt = parse_dt_str(dt_str)
    if not dt:
        return True
    return now_moscow() < dt - timedelta(hours=CANCEL_HOURS)


def get_col_indices(headers):
    try:
        return {
            "phone":  headers.index("Телефон"),
            "doctor": headers.index("Врач"),
            "dt":     headers.index("Дата и время"),
            "status": headers.index("Статус"),
        }
    except ValueError:
        return {"phone": 2, "doctor": 3, "dt": 4, "status": 5}


def get_busy_slots():
    all_values = get_sheet().get_all_values()
    if not all_values:
        return set()
    idx  = get_col_indices([h.strip() for h in all_values[0]])
    busy = set()
    for row in all_values[1:]:
        while len(row) <= max(idx.values()):
            row.append("")
        if row[idx["status"]].strip() not in ("Подтверждено", "Перенесено"):
            continue
        dt = parse_dt_str(row[idx["dt"]])
        if dt:
            busy.add(dt.strftime("%Y-%m-%d %H:%M"))
    return busy


def generate_slots():
    now  = now_moscow()
    busy = get_busy_slots()
    slots = []
    for day_offset in range(DAYS_AHEAD):
        day = (now + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in range(WORK_START, WORK_END):
            slot_dt = day.replace(hour=hour, minute=0)
            if slot_dt <= now:
                continue
            key = slot_dt.strftime("%Y-%m-%d %H:%M")
            slots.append({
                "datetime":  key,
                "date":      slot_dt.strftime("%d.%m.%Y"),
                "time":      slot_dt.strftime("%H:%M"),
                "day_label": slot_dt.strftime("%d.%m"),
                "weekday":   ["пн","вт","ср","чт","пт","сб","вс"][slot_dt.weekday()],
                "available": key not in busy,
            })
    return slots


def find_row(all_values, phone_clean, doctor, dt_raw):
    """Ищет строку по телефону, врачу и дате."""
    if not all_values:
        return None
    idx = get_col_indices([h.strip() for h in all_values[0]])
    for i, row in enumerate(all_values[1:], start=2):
        while len(row) <= max(idx.values()):
            row.append("")
        rp = re.sub(r'\D', '', row[idx["phone"]])
        if (rp == phone_clean
                and row[idx["doctor"]] == doctor
                and row[idx["dt"]].strip().lstrip("'").strip() == dt_raw.strip()
                and row[idx["status"]] in ("Подтверждено", "Перенесено")):
            return i, row, idx
    return None


def find_row_any_date(all_values, phone_clean, doctor):
    """Ищет строку по телефону и врачу без проверки даты — для отмены перенесённых."""
    if not all_values:
        return None
    idx = get_col_indices([h.strip() for h in all_values[0]])
    for i, row in enumerate(all_values[1:], start=2):
        while len(row) <= max(idx.values()):
            row.append("")
        rp = re.sub(r'\D', '', row[idx["phone"]])
        if (rp == phone_clean
                and row[idx["doctor"]] == doctor
                and row[idx["status"]] in ("Подтверждено", "Перенесено")):
            return i, row, idx
    return None


def get_daily_stats():
    """Считает статистику за сегодня и всего активных записей."""
    try:
        all_values = get_sheet().get_all_values()
        if not all_values:
            return 0, 0, 0, 0
        idx   = get_col_indices([h.strip() for h in all_values[0]])
        today = now_moscow().strftime("%d.%m.%Y")
        new_cnt = cancel_cnt = reschedule_cnt = total_active = 0
        for row in all_values[1:]:
            while len(row) <= max(idx.values()):
                row.append("")
            status = row[idx["status"]].strip()
            dt_raw = row[idx["dt"]].strip().lstrip("'")
            if today in dt_raw:
                if status == "Подтверждено":
                    new_cnt += 1
                elif status == "Отменено":
                    cancel_cnt += 1
                elif status == "Перенесено":
                    reschedule_cnt += 1
            if status in ("Подтверждено", "Перенесено"):
                total_active += 1
        return new_cnt, cancel_cnt, reschedule_cnt, total_active
    except Exception as e:
        print(f"[STATS ERROR] {e}")
        return 0, 0, 0, 0


# ── Фоновые задачи ───────────────────────────────────────────────────────────

async def daily_stats_loop():
    """Каждый день в 20:00 МСК отправляет статистику в Telegram."""
    while True:
        try:
            now    = now_moscow()
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            new_cnt, cancel_cnt, reschedule_cnt, total_active = get_daily_stats()
            await tg_send(
                f"📊 <b>СТАТИСТИКА МИНИ-АПП — {now_moscow().strftime('%d.%m.%Y')}</b>\n\n"
                f"✅ Новых записей: {new_cnt}\n"
                f"🗑 Отмен: {cancel_cnt}\n"
                f"🔄 Переносов: {reschedule_cnt}\n"
                f"📋 Всего активных: {total_active}\n\n"
                f"📍 Доктор Смайл, Москва"
            )
        except Exception as e:
            print(f"[STATS LOOP ERROR] {e}")
            await asyncio.sleep(3600)


async def heartbeat_loop():
    """Каждые 6 часов отправляет сигнал что сервер жив."""
    await asyncio.sleep(60)  # небольшая задержка при старте
    while True:
        await tg_send(
            f"✅ <b>Мини-апп работает</b>\n"
            f"🕐 {now_moscow().strftime('%d.%m.%Y %H:%M')}\n"
            f"📍 Доктор Смайл, Москва"
        )
        await asyncio.sleep(6 * 3600)


# ── Lifespan (запуск/остановка фоновых задач) ────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    # Уведомление о старте
    asyncio.create_task(tg_send(
        f"🚀 <b>Мини-апп запущен!</b>\n"
        f"🕐 {now_moscow().strftime('%d.%m.%Y %H:%M')}\n"
        f"📍 Доктор Смайл, Москва"
    ))
    # Фоновые задачи
    t1 = asyncio.create_task(daily_stats_loop())
    t2 = asyncio.create_task(heartbeat_loop())
    yield
    t1.cancel()
    t2.cancel()


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Модели ───────────────────────────────────────────────────────────────────

class BookingRequest(BaseModel):
    doctor_id: int
    datetime:  str
    name:      str
    phone:     str

class ChatRequest(BaseModel):
    message: str

class CancelRequest(BaseModel):
    phone:    str
    doctor:   str
    datetime: str

class RescheduleRequest(BaseModel):
    phone:        str
    doctor:       str
    old_datetime: str
    new_datetime: str


# ── Эндпоинты ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "VK Mini App API — Доктор Смайл"}


@app.get("/api/doctors")
def get_doctors():
    return {"doctors": DOCTORS}


@app.get("/api/slots")
def get_slots():
    try:
        return {"slots": generate_slots()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/book")
async def book(req: BookingRequest):
    try:
        doctor = next((d for d in DOCTORS if d["id"] == req.doctor_id), None)
        if not doctor:
            raise HTTPException(status_code=400, detail="Врач не найден")
        busy   = get_busy_slots()
        dt_obj = datetime.strptime(req.datetime, "%Y-%m-%d %H:%M")
        if dt_obj.strftime("%Y-%m-%d %H:%M") in busy:
            raise HTTPException(status_code=409, detail="Это время уже занято")
        parts      = req.name.strip().split(maxsplit=1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ""
        get_sheet().append_row([
            first_name, last_name, req.phone,
            doctor["name"],
            dt_obj.strftime("%d.%m.%Y %H:%M"),
            "Подтверждено", ""
        ])
        # Уведомление в Telegram
        await tg_send(
            f"🦷 <b>НОВАЯ ЗАПИСЬ (мини-апп)</b>\n"
            f"👤 {req.name}\n"
            f"📞 {req.phone}\n"
            f"👨‍⚕️ {doctor['name']}\n"
            f"🕐 {dt_obj.strftime('%d.%m.%Y в %H:%M')}\n"
            f"📍 Доктор Смайл, Москва"
        )
        return {
            "success": True,
            "booking": {
                "name":     req.name,
                "phone":    req.phone,
                "doctor":   doctor["name"],
                "datetime": dt_obj.strftime("%d.%m.%Y в %H:%M"),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/my_bookings")
def my_bookings(phone: str):
    try:
        all_values  = get_sheet().get_all_values()
        if not all_values:
            return {"bookings": []}
        idx         = get_col_indices([h.strip() for h in all_values[0]])
        phone_clean = re.sub(r'\D', '', phone)
        result = []
        for row in all_values[1:]:
            while len(row) <= max(idx.values()):
                row.append("")
            if re.sub(r'\D', '', row[idx["phone"]]) == phone_clean:
                dt_raw = row[idx["dt"]].strip().lstrip("'").strip()
                result.append({
                    "doctor":     row[idx["doctor"]],
                    "datetime":   dt_raw,
                    "status":     row[idx["status"]],
                    "can_modify": can_modify(dt_raw),
                })
        return {"bookings": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cancel_booking")
async def cancel_booking(req: CancelRequest):
    try:
        sheet       = get_sheet()
        all_values  = sheet.get_all_values()
        phone_clean = re.sub(r'\D', '', req.phone)
        # Сначала ищем по дате, если не нашли — ищем без даты (перенесённые)
        found = find_row(all_values, phone_clean, req.doctor, req.datetime)
        if not found:
            found = find_row_any_date(all_values, phone_clean, req.doctor)
        if not found:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        row_i, row, idx = found
        if not can_modify(row[idx["dt"]]):
            raise HTTPException(
                status_code=403,
                detail=f"Отменить можно не позднее чем за {CANCEL_HOURS} ч до приёма. Позвоните нам: +7 (495) 123-45-67"
            )
        sheet.update_cell(row_i, idx["status"] + 1, "Отменено")
        # Уведомление в Telegram
        await tg_send(
            f"🗑 <b>ОТМЕНА ЗАПИСИ (мини-апп)</b>\n"
            f"📞 {req.phone}\n"
            f"👨‍⚕️ {req.doctor}\n"
            f"🕐 Была: {req.datetime}\n"
            f"📍 Доктор Смайл, Москва"
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reschedule_booking")
async def reschedule_booking(req: RescheduleRequest):
    try:
        sheet       = get_sheet()
        all_values  = sheet.get_all_values()
        phone_clean = re.sub(r'\D', '', req.phone)
        found = find_row(all_values, phone_clean, req.doctor, req.old_datetime)
        if not found:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        row_i, row, idx = found
        if not can_modify(row[idx["dt"]]):
            raise HTTPException(
                status_code=403,
                detail=f"Перенести можно не позднее чем за {CANCEL_HOURS} ч до приёма. Позвоните нам: +7 (495) 123-45-67"
            )
        busy   = get_busy_slots()
        dt_obj = datetime.strptime(req.new_datetime, "%Y-%m-%d %H:%M")
        if dt_obj.strftime("%Y-%m-%d %H:%M") in busy:
            raise HTTPException(status_code=409, detail="Это время уже занято")
        new_dt_str = dt_obj.strftime("%d.%m.%Y %H:%M")
        sheet.update_cell(row_i, idx["dt"]     + 1, new_dt_str)
        sheet.update_cell(row_i, idx["status"] + 1, "Перенесено")
        # Уведомление в Telegram
        await tg_send(
            f"🔄 <b>ПЕРЕНОС ЗАПИСИ (мини-апп)</b>\n"
            f"📞 {req.phone}\n"
            f"👨‍⚕️ {req.doctor}\n"
            f"🕐 Было: {req.old_datetime}\n"
            f"🕐 Стало: {new_dt_str}\n"
            f"📍 Доктор Смайл, Москва"
        )
        return {"success": True, "new_datetime": dt_obj.strftime("%d.%m.%Y в %H:%M")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                json={
                    "model": "mistralai/mistral-small-3.2-24b-instruct",
                    "messages": [
                        {"role": "system", "content": CLINIC_CONTEXT},
                        {"role": "user",   "content": req.message}
                    ],
                    "max_tokens": 300
                },
                timeout=15
            )
            data = r.json()
            return {"reply": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug")
def debug():
    all_values = get_sheet().get_all_values()
    return {
        "headers":    all_values[0] if all_values else [],
        "rows":       all_values[1:],
        "busy_slots": sorted(list(get_busy_slots())),
        "now_moscow": now_moscow().strftime("%d.%m.%Y %H:%M"),
    }


# ── Админ-панель ─────────────────────────────────────────────────────────────

ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD", "doktor2026")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "вкстомат тест")


@app.get("/api/admin/all_bookings")
def admin_all_bookings(password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
    try:
        all_values = get_sheet().get_all_values()
        if not all_values:
            return {"bookings": [], "stats": {}}
        headers   = [h.strip() for h in all_values[0]]
        idx       = get_col_indices(headers)
        idx_first = headers.index("Имя")     if "Имя"     in headers else 0
        idx_last  = headers.index("Фамилия") if "Фамилия" in headers else 1

        bookings = []
        today = now_moscow().strftime("%d.%m.%Y")
        stats = {"total": 0, "confirmed": 0, "cancelled": 0, "rescheduled": 0, "today": 0}

        for i, row in enumerate(all_values[1:], start=2):
            while len(row) <= max(idx.values()):
                row.append("")
            dt_raw = row[idx["dt"]].strip().lstrip("'").strip()
            status = row[idx["status"]].strip()
            dt_obj = parse_dt_str(dt_raw)

            stats["total"] += 1
            if status == "Подтверждено":  stats["confirmed"]   += 1
            if status == "Отменено":      stats["cancelled"]   += 1
            if status == "Перенесено":    stats["rescheduled"] += 1
            if today in dt_raw and status in ("Подтверждено", "Перенесено"):
                stats["today"] += 1

            bookings.append({
                "row":        i,
                "name":       f"{row[idx_first]} {row[idx_last]}".strip(),
                "phone":      row[idx["phone"]],
                "doctor":     row[idx["doctor"]],
                "datetime":   dt_raw,
                "dt_sort":    dt_obj.strftime("%Y-%m-%d %H:%M") if dt_obj else "9999",
                "status":     status,
                "can_modify": can_modify(dt_raw),
            })

        bookings.sort(key=lambda x: x["dt_sort"])
        return {"bookings": bookings, "stats": stats, "now": now_moscow().strftime("%d.%m.%Y %H:%M")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AdminStatusUpdate(BaseModel):
    password: str
    row:      int
    status:   str


@app.post("/api/admin/update_status")
async def admin_update_status(req: AdminStatusUpdate):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
    if req.status not in ("Подтверждено", "Отменено", "Перенесено"):
        raise HTTPException(status_code=400, detail="Недопустимый статус")
    try:
        sheet      = get_sheet()
        all_values = sheet.get_all_values()
        idx        = get_col_indices([h.strip() for h in all_values[0]])
        sheet.update_cell(req.row, idx["status"] + 1, req.status)
        row = all_values[req.row - 1]
        while len(row) <= max(idx.values()):
            row.append("")
        await tg_send(
            f"✏️ <b>СТАТУС ИЗМЕНЁН (админ)</b>\n"
            f"📞 {row[idx['phone']]}\n"
            f"👨‍⚕️ {row[idx['doctor']]}\n"
            f"🕐 {row[idx['dt']]}\n"
            f"📋 Новый статус: {req.status}"
        )
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
