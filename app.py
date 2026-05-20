from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz
import re
import httpx
import json
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MOSCOW_TZ = pytz.timezone("Europe/Moscow")
SPREADSHEET_NAME = "вкстомат тест"
WORK_START = 8
WORK_END = 20
DAYS_AHEAD = 14

OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

CLINIC_CONTEXT ="""Ты ассистент стоматологической клиники «Доктор Смайл».
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


def get_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).sheet1


def parse_dt_str(dt_str: str):
    """Парсит строку даты в любом из известных форматов. Возвращает datetime или None."""
    # Убираем апостроф, пробелы, невидимые символы
    dt_str = dt_str.strip().lstrip("'").strip()
    if not dt_str:
        return None
    for fmt in (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


def get_busy_slots():
    sheet = get_sheet()
    # get_all_values() надёжнее get_all_records() при пустых ячейках
    all_values = sheet.get_all_values()
    if not all_values:
        return set()

    headers = [h.strip() for h in all_values[0]]
    busy = set()

    # Ищем индексы нужных колонок по заголовку
    try:
        idx_status = headers.index("Статус")
        idx_dt = headers.index("Дата и время")
    except ValueError:
        # Если заголовки не найдены — fallback на фиксированные индексы (E=4, F=5)
        idx_dt = 4
        idx_status = 5

    for row in all_values[1:]:
        # Дополняем короткие строки пустыми значениями
        while len(row) <= max(idx_status, idx_dt):
            row.append("")

        status = row[idx_status].strip()
        if status not in ("Подтверждено", "Перенесено"):
            continue

        dt = parse_dt_str(row[idx_dt])
        if dt:
            busy.add(dt.strftime("%Y-%m-%d %H:%M"))

    return busy


def generate_slots():
    now = datetime.now(MOSCOW_TZ).replace(tzinfo=None)
    busy = get_busy_slots()
    slots = []
    for day_offset in range(DAYS_AHEAD):
        day = (now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for hour in range(WORK_START, WORK_END):
            slot_dt = day.replace(hour=hour, minute=0)
            if slot_dt <= now:
                continue
            key = slot_dt.strftime("%Y-%m-%d %H:%M")
            slots.append({
                "datetime": key,
                "date": slot_dt.strftime("%d.%m.%Y"),
                "time": slot_dt.strftime("%H:%M"),
                "day_label": slot_dt.strftime("%d.%m"),
                "weekday": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][slot_dt.weekday()],
                "available": key not in busy
            })
    return slots


class BookingRequest(BaseModel):
    doctor_id: int
    datetime: str
    name: str
    phone: str


class ChatRequest(BaseModel):
    message: str


# ── Эндпоинты ──────────────────────────────────────────────────────────────

_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Доктор Смайл</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
<style>
/* ── Variables ── */
:root {
  --bg:          #020c1b;
  --bg2:         #051628;
  --bg3:         #091e35;
  --glass:       rgba(5, 18, 40, 0.55);
  --glass2:      rgba(9, 30, 53, 0.65);
  --glass-modal: rgba(3, 10, 26, 0.88);
  --text:        #e2efff;
  --text2:       #6fa3cc;
  --text3:       #2d5577;
  --border:      rgba(255, 255, 255, 0.07);
  --border-blue: rgba(74, 168, 255, 0.18);
  --border-hover:rgba(255, 255, 255, 0.14);
  --accent:      #4aa8ff;
  --accent2:     #2888e8;
  --success:     #30d158;
  --danger:      #ff453a;
  --warn:        #ff9f0a;
  --shadow-sm:   0 2px 8px rgba(0,0,0,0.3);
  --shadow:      0 8px 32px rgba(0,0,0,0.5), 0 2px 8px rgba(0,0,0,0.25);
  --shadow-lg:   0 16px 48px rgba(0,0,0,0.6), 0 4px 16px rgba(0,0,0,0.3);
  --glow:        0 0 28px rgba(74,168,255,0.22);
  --radius:      16px;
  --ease-expo:   cubic-bezier(0.16, 1, 0.3, 1);
  --ease-spring: cubic-bezier(0.175, 0.885, 0.32, 1.275);
}

/* ── Reset ── */
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background:
    radial-gradient(ellipse 80% 60% at 10% 5%,  rgba(14, 60, 140, 0.40) 0%, transparent 55%),
    radial-gradient(ellipse 60% 50% at 90% 90%, rgba(5, 28, 100, 0.32) 0%, transparent 55%),
    radial-gradient(ellipse 40% 40% at 50% 50%, rgba(8, 25, 80, 0.15) 0%, transparent 70%),
    #020c1b;
  background-attachment: fixed;
  color: var(--text);
  min-height: 100vh;
  font-size: 14px;
}

/* ── Glass mixin ── */
.glass {
  background: var(--glass);
  backdrop-filter: blur(35px) saturate(180%);
  -webkit-backdrop-filter: blur(35px) saturate(180%);
  border: 1px solid var(--border);
  box-shadow:
    0 1px 0 rgba(255,255,255,0.06) inset,
    0 -1px 0 rgba(0,0,0,0.20) inset,
    var(--shadow);
}

/* ── Header ── */
.header {
  background: rgba(2, 8, 22, 0.80);
  backdrop-filter: blur(40px) saturate(200%);
  -webkit-backdrop-filter: blur(40px) saturate(200%);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 4px 20px rgba(0,0,0,0.35);
  color: var(--text);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 100;
}

/* ── Animated clickable logo ── */
.header-logo-btn {
  width: 42px; height: 42px; flex-shrink: 0;
  background: linear-gradient(145deg, rgba(74,168,255,0.22), rgba(74,168,255,0.06));
  border: 1px solid rgba(74,168,255,0.35);
  border-radius: 13px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; color: var(--accent);
  transition: transform 0.35s var(--ease-expo), box-shadow 0.35s ease, border-color 0.2s ease;
  animation: logo-pulse 3.2s ease-in-out infinite;
  -webkit-animation: logo-pulse 3.2s ease-in-out infinite;
}
.header-logo-btn svg { width: 22px; height: 22px; }
.header-logo-btn:active { transform: scale(0.88) !important; animation-play-state: paused; }
.header-logo-btn:hover  { border-color: rgba(74,168,255,0.55); box-shadow: 0 0 20px rgba(74,168,255,0.30); animation-play-state: paused; }

@keyframes logo-pulse {
  0%,100% { box-shadow: 0 0 0 0 rgba(74,168,255,0.0); }
  45%     { box-shadow: 0 0 0 5px rgba(74,168,255,0.16), 0 0 18px rgba(74,168,255,0.22); }
}

.header-title { font-size: 16px; font-weight: 700; letter-spacing: -0.3px; color: var(--text); }
.header-sub   { font-size: 11px; color: var(--text2); margin-top: 1px; }
.header-badge {
  margin-left: auto;
  background: rgba(74,168,255,0.10);
  border: 1px solid rgba(74,168,255,0.22);
  padding: 4px 11px; border-radius: 20px;
  font-size: 11px; font-weight: 600; color: var(--accent);
  white-space: nowrap;
}

/* ── Nav ── */
.nav {
  display: flex;
  background: rgba(2, 8, 22, 0.78);
  backdrop-filter: blur(40px) saturate(200%);
  -webkit-backdrop-filter: blur(40px) saturate(200%);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.03) inset;
  position: sticky; top: 66px; z-index: 99;
}
.nav-btn {
  flex: 1; padding: 10px 4px 9px;
  text-align: center;
  font-size: 10px; font-weight: 500; color: var(--text3);
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color 0.25s var(--ease-expo), border-color 0.25s var(--ease-expo);
  display: flex; flex-direction: column; align-items: center; gap: 4px;
}
.nav-btn .ico {
  width: 22px; height: 22px;
  display: flex; align-items: center; justify-content: center;
  transition: transform 0.4s var(--ease-expo);
}
.nav-btn .ico svg { width: 20px; height: 20px; stroke: currentColor; }
.nav-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.nav-btn.active .ico { transform: scale(1.14); }

/* ── Pages ── */
.page { display: none; padding-bottom: 28px; }
.page.active { display: block; animation: fadeUp 0.45s var(--ease-expo) both; }
@keyframes fadeUp { from{opacity:0;transform:translateY(14px)} to{opacity:1;transform:translateY(0)} }

/* ── Glass Section / Card ── */
.section {
  background: var(--glass);
  backdrop-filter: blur(35px) saturate(180%);
  -webkit-backdrop-filter: blur(35px) saturate(180%);
  border: 1px solid var(--border);
  box-shadow:
    0 1px 0 rgba(255,255,255,0.06) inset,
    var(--shadow);
  margin: 10px;
  border-radius: var(--radius);
  padding: 16px;
  transition: border-color 0.25s ease, box-shadow 0.25s ease;
}
.section + .section {
  margin-top: 0;
  border-top-left-radius: 0; border-top-right-radius: 0;
  border-top: 1px solid var(--border);
}
.section:hover { border-color: var(--border-hover); }

.section-label {
  font-size: 10px; font-weight: 700;
  color: var(--text3); text-transform: uppercase;
  letter-spacing: 1px; margin-bottom: 14px;
}

/* ── Doctor cards ── */
.doctor-card {
  border: 1px solid var(--border); border-radius: 14px; padding: 13px;
  display: flex; align-items: center; gap: 12px;
  cursor: pointer; margin-bottom: 8px;
  background: var(--glass2);
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  transition: transform 0.35s var(--ease-expo), border-color 0.2s ease, box-shadow 0.25s ease;
}
.doctor-card:last-child { margin-bottom: 0; }
.doctor-card:active { transform: scale(0.97); transition-duration: 0.08s; }
.doctor-card.selected {
  border-color: rgba(74,168,255,0.45);
  background: rgba(74,168,255,0.08);
  box-shadow: 0 0 0 1px rgba(74,168,255,0.20), var(--glow);
}
.avatar {
  width: 46px; height: 46px; border-radius: 14px;
  background: linear-gradient(135deg, rgba(74,168,255,0.22), rgba(74,168,255,0.06));
  border: 1px solid rgba(74,168,255,0.25);
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700; color: var(--accent); flex-shrink: 0;
}
.doc-name { font-size: 14px; font-weight: 600; color: var(--text); }
.doc-spec  { font-size: 12px; color: var(--text2); margin-top: 2px; }
.check {
  margin-left: auto; width: 24px; height: 24px;
  border-radius: 50%; background: var(--accent);
  display: none; align-items: center; justify-content: center;
  color: white; flex-shrink: 0;
  box-shadow: 0 0 14px rgba(74,168,255,0.55);
}
.check svg { width: 13px; height: 13px; }
.doctor-card.selected .check { display: flex; }

/* ── Date chips ── */
.dates-scroll { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; -webkit-overflow-scrolling: touch; }
.dates-scroll::-webkit-scrollbar { display: none; }
.date-chip {
  min-width: 52px; text-align: center; padding: 9px 6px; border-radius: 14px;
  border: 1px solid var(--border); cursor: pointer; flex-shrink: 0;
  background: var(--glass2);
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  transition: transform 0.35s var(--ease-expo), border-color 0.2s ease, box-shadow 0.2s ease;
}
.date-chip:active { transform: scale(0.93); transition-duration: 0.08s; }
.date-chip.selected {
  background: linear-gradient(145deg, #3da5f5, #1e7ad8);
  border-color: rgba(74,168,255,0.6);
  box-shadow: 0 4px 18px rgba(74,168,255,0.45), 0 1px 0 rgba(255,255,255,0.12) inset;
}
.date-chip .wday { font-size: 10px; font-weight: 600; color: var(--text3); text-transform: uppercase; }
.date-chip.selected .wday { color: rgba(255,255,255,0.75); }
.date-chip .dnum { font-size: 18px; font-weight: 700; color: var(--text); margin: 1px 0; }
.date-chip.selected .dnum { color: white; }
.date-chip .mon  { font-size: 10px; color: var(--text3); }
.date-chip.selected .mon  { color: rgba(255,255,255,0.75); }

/* ── Time slots ── */
.slots-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.slot {
  text-align: center; padding: 11px 4px; border-radius: 12px;
  border: 1px solid var(--border); font-size: 13px; font-weight: 500;
  cursor: pointer; color: var(--text);
  background: var(--glass2);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  transition: transform 0.35s var(--ease-expo), border-color 0.2s ease, box-shadow 0.2s ease;
}
.slot:active { transform: scale(0.92); transition-duration: 0.07s; }
.slot.selected {
  background: linear-gradient(145deg, #3da5f5, #1e7ad8);
  border-color: rgba(74,168,255,0.6); color: white; font-weight: 700;
  box-shadow: 0 4px 16px rgba(74,168,255,0.45), 0 1px 0 rgba(255,255,255,0.12) inset;
}
.slot.busy { background: rgba(9,30,53,0.20); color: var(--text3); cursor: not-allowed; border-color: transparent; opacity: 0.4; }

/* ── Inputs ── */
.input-group { margin-bottom: 12px; }
.input-group:last-child { margin-bottom: 0; }
.input-group label {
  font-size: 11px; font-weight: 600; color: var(--text2);
  display: block; margin-bottom: 7px; text-transform: uppercase; letter-spacing: 0.5px;
}
.input-group input {
  width: 100%; padding: 13px 14px;
  border: 1px solid var(--border); border-radius: 12px;
  font-size: 15px; font-family: inherit; outline: none;
  background: rgba(5,18,40,0.50);
  backdrop-filter: blur(20px) saturate(150%);
  -webkit-backdrop-filter: blur(20px) saturate(150%);
  color: var(--text);
  transition: border-color 0.25s ease, box-shadow 0.25s ease, background 0.25s ease;
}
.input-group input::placeholder { color: var(--text3); }
.input-group input:focus {
  border-color: rgba(74,168,255,0.50);
  background: rgba(5,18,40,0.72);
  box-shadow: 0 0 0 3px rgba(74,168,255,0.12), 0 1px 0 rgba(255,255,255,0.04) inset;
}

/* ── Summary box ── */
.summary-box {
  background: rgba(9,30,53,0.50);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 14px; padding: 14px; margin-bottom: 16px;
  border: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
}
.srow {
  display: flex; justify-content: space-between; align-items: center;
  padding: 7px 0; font-size: 13px;
  border-bottom: 1px solid rgba(255,255,255,0.05);
}
.srow:last-child { border-bottom: none; padding-bottom: 0; }
.srow:first-child { padding-top: 0; }
.srow .lbl {
  color: var(--text2); font-weight: 500;
  display: flex; align-items: center; gap: 6px;
}
.srow .lbl svg { opacity: 0.7; flex-shrink: 0; }
.srow .val { font-weight: 600; color: var(--text); text-align: right; max-width: 60%; }

/* ── Buttons ── */
.btn {
  width: 100%; padding: 15px; border: none; border-radius: 14px;
  font-size: 15px; font-weight: 700; font-family: inherit; cursor: pointer;
  transition: transform 0.4s var(--ease-expo), box-shadow 0.25s ease, opacity 0.15s ease;
  will-change: transform;
}
.btn:active { transform: scale(0.97); transition-duration: 0.08s, 0.08s, 0.08s; }
.btn-p {
  background: linear-gradient(145deg, #3da5f5 0%, #1e7ad8 100%);
  color: white;
  box-shadow: 0 4px 20px rgba(74,168,255,0.38), 0 1px 0 rgba(255,255,255,0.10) inset;
}
.btn-p:hover { box-shadow: 0 6px 28px rgba(74,168,255,0.50), 0 1px 0 rgba(255,255,255,0.10) inset; }
.btn-p:disabled {
  background: rgba(9,30,53,0.55); color: var(--text3);
  cursor: not-allowed; transform: none !important; box-shadow: none;
  border: 1px solid var(--border);
}
.btn-s {
  background: rgba(9,30,53,0.45); color: var(--text2); margin-top: 8px;
  border: 1px solid var(--border);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
}
.btn-s:hover { border-color: var(--border-hover); color: var(--text); }

/* ── Success screen ── */
.success-screen { text-align: center; padding: 32px 16px; }
.success-icon {
  font-size: 60px; margin-bottom: 14px; display: block;
  animation: pop 0.5s var(--ease-spring) both;
}
@keyframes pop { 0%{transform:scale(0);opacity:0} 100%{transform:scale(1);opacity:1} }
.success-title { font-size: 22px; font-weight: 800; margin-bottom: 6px; color: var(--text); }
.success-sub   { font-size: 13px; color: var(--text2); line-height: 1.6; }
.success-card {
  background: rgba(74,168,255,0.07);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(74,168,255,0.18);
  border-radius: 16px; padding: 16px; margin: 18px 0; text-align: left;
  box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;
}
.info-row {
  display: flex; gap: 10px; align-items: center; padding: 7px 0;
  border-bottom: 1px solid rgba(74,168,255,0.08); font-size: 13px;
}
.info-row:last-child { border-bottom: none; padding-bottom: 0; }
.info-row:first-child { padding-top: 0; }
.info-icon { color: var(--accent); flex-shrink: 0; display: flex; align-items: center; }
.info-icon svg { width: 15px; height: 15px; }
.info-label { color: var(--text2); font-weight: 500; flex: 1; }
.info-val   { font-weight: 700; color: var(--text); text-align: right; }

/* ── Chat ── */
.chat-wrap { display: flex; flex-direction: column; height: calc(100vh - 130px); }
.chat-msgs {
  flex: 1; overflow-y: auto; padding: 12px;
  display: flex; flex-direction: column; gap: 10px; scroll-behavior: smooth;
}
.chat-msgs::-webkit-scrollbar { width: 3px; }
.chat-msgs::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
.msg {
  max-width: 78%; padding: 11px 15px;
  border-radius: 18px; font-size: 14px; line-height: 1.55;
  animation: msgIn 0.3s var(--ease-expo) both;
  white-space: pre-line; word-break: break-word;
}
@keyframes msgIn { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
.msg-bot {
  background: var(--glass);
  backdrop-filter: blur(35px) saturate(180%);
  -webkit-backdrop-filter: blur(35px) saturate(180%);
  border: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset, var(--shadow-sm);
  color: var(--text); border-bottom-left-radius: 5px; align-self: flex-start;
}
.msg-user {
  background: linear-gradient(145deg, #3da5f5, #1e7ad8);
  color: white; border-bottom-right-radius: 5px; align-self: flex-end;
  box-shadow: 0 4px 16px rgba(74,168,255,0.40), 0 1px 0 rgba(255,255,255,0.12) inset;
}
.typing-dots { display: flex; gap: 4px; align-items: center; padding: 4px 0; }
.typing-dots span {
  width: 7px; height: 7px; background: var(--text3); border-radius: 50%;
  animation: dot 1.2s infinite;
}
.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes dot { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-5px)} }
.chat-bottom {
  background: rgba(2,8,22,0.82);
  backdrop-filter: blur(40px) saturate(200%);
  -webkit-backdrop-filter: blur(40px) saturate(200%);
  border-top: 1px solid var(--border); flex-shrink: 0;
}
.chat-bar { display: flex; gap: 8px; padding: 10px 12px; }
.chat-bar input {
  flex: 1; padding: 11px 16px;
  border: 1px solid var(--border); border-radius: 24px;
  font-size: 14px; font-family: inherit; outline: none;
  background: rgba(9,30,53,0.50);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  color: var(--text); transition: border-color 0.2s ease;
}
.chat-bar input::placeholder { color: var(--text3); }
.chat-bar input:focus { border-color: rgba(74,168,255,0.45); }
.send-btn {
  width: 42px; height: 42px; border-radius: 50%;
  background: linear-gradient(145deg, #3da5f5, #1e7ad8);
  border: none; color: white; font-size: 17px; cursor: pointer;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  box-shadow: 0 2px 14px rgba(74,168,255,0.45), 0 1px 0 rgba(255,255,255,0.12) inset;
  transition: transform 0.4s var(--ease-expo), box-shadow 0.2s ease;
}
.send-btn svg { width: 18px; height: 18px; }
.send-btn:active { transform: scale(0.88); transition-duration: 0.07s; }
.quick-btns { display: flex; flex-wrap: wrap; gap: 7px; padding: 0 12px 10px; }
.qbtn {
  padding: 7px 13px;
  border: 1px solid rgba(74,168,255,0.28); border-radius: 22px;
  font-size: 12px; font-weight: 600; font-family: inherit; color: var(--accent);
  cursor: pointer;
  background: rgba(74,168,255,0.07);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  transition: transform 0.35s var(--ease-expo), background 0.2s ease, border-color 0.2s ease;
  white-space: nowrap;
}
.qbtn:active { background: var(--accent); color: white; transform: scale(0.94); transition-duration: 0.07s; }
.qbtn:hover  { border-color: rgba(74,168,255,0.50); }

/* ── My bookings cards ── */
.bcard {
  border: 1px solid var(--border); border-radius: 14px; padding: 14px; margin-bottom: 10px;
  background: var(--glass);
  backdrop-filter: blur(35px) saturate(180%);
  -webkit-backdrop-filter: blur(35px) saturate(180%);
  box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset, var(--shadow-sm);
  cursor: pointer;
  transition: transform 0.4s var(--ease-expo), box-shadow 0.25s ease, border-color 0.2s ease;
}
.bcard:active { transform: scale(0.97); transition-duration: 0.08s; }
.bcard:hover  { box-shadow: 0 6px 24px rgba(74,168,255,0.18), 0 1px 0 rgba(255,255,255,0.05) inset; border-color: var(--border-hover); }
.bstatus { display: inline-flex; align-items: center; gap: 5px; padding: 4px 11px; border-radius: 22px; font-size: 11px; font-weight: 700; margin-bottom: 10px; }
.st-ok { background: rgba(48,209,88,0.12);  color: var(--success); }
.st-no { background: rgba(255,69,58,0.12);  color: var(--danger); }
.st-mv { background: rgba(255,159,10,0.12); color: var(--warn); }

/* ── Info grid ── */
.info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.info-tile {
  background: rgba(9,30,53,0.50);
  backdrop-filter: blur(20px) saturate(150%);
  -webkit-backdrop-filter: blur(20px) saturate(150%);
  border-radius: 12px; padding: 12px; border: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
  transition: border-color 0.2s ease;
}
.info-tile:hover { border-color: var(--border-hover); }
.info-tile-icon { color: var(--accent); margin-bottom: 5px; }
.info-tile-icon svg { width: 18px; height: 18px; }
.info-tile-label { font-size: 11px; color: var(--text2); font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; }
.info-tile-val   { font-size: 13px; font-weight: 600; margin-top: 3px; color: var(--text); }
.info-tile-val.accent { color: var(--accent); }

/* ── Loading / Empty ── */
.loading { text-align: center; padding: 28px; color: var(--text3); font-size: 13px; }
.loading::before {
  content: ''; display: block; width: 24px; height: 24px;
  border: 2px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.7s linear infinite; margin: 0 auto 10px;
}
@keyframes spin { to{transform:rotate(360deg)} }
.empty { text-align: center; padding: 48px 20px; color: var(--text3); }
.empty .ei { margin-bottom: 12px; display: block; opacity: 0.4; }
.empty .ei svg { width: 44px; height: 44px; }
.empty p { font-size: 14px; }
.hidden { display: none !important; }

/* ── Modals ── */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(1, 4, 14, 0.75);
  backdrop-filter: blur(6px) saturate(140%);
  -webkit-backdrop-filter: blur(6px) saturate(140%);
  z-index: 200; display: flex; align-items: flex-end; justify-content: center;
  animation: fadeIn 0.25s ease both;
}
.modal-overlay.hidden { display: none !important; }
@keyframes fadeIn { from{opacity:0} to{opacity:1} }

.modal-sheet {
  background: var(--glass-modal);
  backdrop-filter: blur(45px) saturate(210%);
  -webkit-backdrop-filter: blur(45px) saturate(210%);
  border: 1px solid rgba(255,255,255,0.09);
  border-bottom: none;
  box-shadow:
    0 2px 0 rgba(255,255,255,0.07) inset,
    0 -40px 80px rgba(0,0,0,0.65),
    0 -8px 32px rgba(0,0,0,0.40);
  border-radius: 26px 26px 0 0;
  padding: 20px 16px 34px;
  width: 100%; max-width: 480px;
  animation: slideUp 0.44s var(--ease-expo) both;
}
@keyframes slideUp { from{transform:translateY(100%);opacity:0.6} to{transform:translateY(0);opacity:1} }

.modal-handle {
  width: 36px; height: 4px;
  background: rgba(255,255,255,0.14);
  border-radius: 2px; margin: 0 auto 20px;
}
.modal-title { font-size: 17px; font-weight: 700; margin-bottom: 4px; color: var(--text); }
.modal-sub { font-size: 13px; color: var(--text2); margin-bottom: 18px; line-height: 1.5; }
.modal-info {
  background: rgba(9,30,53,0.55);
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  border-radius: 14px; padding: 12px 14px; margin-bottom: 18px;
  border: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
}
.btn-danger {
  background: rgba(255,69,58,0.10); color: var(--danger);
  border: 1px solid rgba(255,69,58,0.28);
}
.btn-danger:hover { background: rgba(255,69,58,0.15); }
.btn-warn {
  background: rgba(255,159,10,0.10); color: var(--warn);
  border: 1px solid rgba(255,159,10,0.28); margin-top: 8px;
}
.btn-warn:hover { background: rgba(255,159,10,0.16); }
.btn-close {
  background: rgba(9,30,53,0.45); color: var(--text2);
  border: 1px solid var(--border); margin-top: 8px;
}

/* ── О клинике modal specifics ── */
.clinic-logo-display {
  width: 72px; height: 72px; border-radius: 22px; margin: 0 auto;
  background: linear-gradient(145deg, rgba(74,168,255,0.24), rgba(74,168,255,0.06));
  border: 1px solid rgba(74,168,255,0.35);
  display: flex; align-items: center; justify-content: center;
  color: var(--accent);
  box-shadow: 0 8px 28px rgba(74,168,255,0.25), 0 1px 0 rgba(255,255,255,0.08) inset;
}
.clinic-logo-display svg { width: 38px; height: 38px; }

.clinic-stats-row {
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 20px;
}
.clinic-stat {
  background: rgba(74,168,255,0.07);
  border: 1px solid rgba(74,168,255,0.16);
  border-radius: 12px; padding: 12px 8px; text-align: center;
}
.clinic-stat-num { font-size: 22px; font-weight: 800; color: var(--accent); line-height: 1; }
.clinic-stat-lbl { font-size: 10px; color: var(--text2); margin-top: 3px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.4px; }

.clinic-features { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
.clinic-feature {
  display: flex; align-items: flex-start; gap: 12px; padding: 12px 14px;
  background: rgba(9,30,53,0.50);
  backdrop-filter: blur(15px); -webkit-backdrop-filter: blur(15px);
  border: 1px solid var(--border); border-radius: 12px;
  box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset;
}
.clinic-feature-icon {
  width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
  background: rgba(74,168,255,0.12);
  border: 1px solid rgba(74,168,255,0.20);
  display: flex; align-items: center; justify-content: center; color: var(--accent);
}
.clinic-feature-icon svg { width: 17px; height: 17px; }
.clinic-feature-title { font-size: 13px; font-weight: 600; color: var(--text); margin-bottom: 2px; }
.clinic-feature-desc  { font-size: 11px; color: var(--text2); line-height: 1.5; }
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="header">
  <button class="header-logo-btn" onclick="openClinicModal()" aria-label="О клинике">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3C9.5 3 7.5 4.5 6.5 7C5.8 5.8 4.6 5 3 5C1.3 7 2.1 10 3.5 12C4.3 13.3 5 14.5 5 17C5 19.5 6 21 7.5 21C9 21 9.5 19 10 17C10.5 15 11 14 12 14C13 14 13.5 15 14 17C14.5 19 15 21 16.5 21C18 21 19 19.5 19 17C19 14.5 19.7 13.3 20.5 12C21.9 10 22.7 7 21 5C19.4 5 18.2 5.8 17.5 7C16.5 4.5 14.5 3 12 3Z"/>
    </svg>
  </button>
  <div>
    <div class="header-title">Доктор Смайл</div>
    <div class="header-sub">ул. Профсоюзная, 87</div>
  </div>
  <div class="header-badge">8:00 – 20:00</div>
</div>

<!-- ── NAV ── -->
<div class="nav">
  <div class="nav-btn active" onclick="showPage('book')" id="nav-book">
    <span class="ico">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2.5"/>
        <path d="M3 10h18M8 2v4M16 2v4"/>
        <circle cx="9" cy="15" r="1.2" fill="currentColor" stroke="none"/>
        <circle cx="12" cy="15" r="1.2" fill="currentColor" stroke="none"/>
        <circle cx="15" cy="15" r="1.2" fill="currentColor" stroke="none"/>
      </svg>
    </span>Запись
  </div>
  <div class="nav-btn" onclick="showPage('chat')" id="nav-chat">
    <span class="ico">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 2H3a1 1 0 0 0-1 1v18l4-4h15a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1z"/>
        <path d="M7 10h10M7 14h6"/>
      </svg>
    </span>Вопросы
  </div>
  <div class="nav-btn" onclick="showPage('my')" id="nav-my">
    <span class="ico">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <rect x="5" y="2" width="14" height="20" rx="2"/>
        <path d="M9 7h6M9 11h6M9 15h4"/>
        <path d="M9 2v3h6V2"/>
      </svg>
    </span>Мои записи
  </div>
</div>

<!-- ── ЗАПИСЬ ── -->
<div class="page active" id="page-book">
  <div id="bs1">
    <div class="section">
      <div class="section-label">Выберите специалиста</div>
      <div id="docs-list"><div class="loading"></div></div>
    </div>
    <div style="padding:10px 10px 0">
      <button class="btn btn-p" id="btn2" disabled onclick="goS(2)">Далее →</button>
    </div>
  </div>

  <div id="bs2" class="hidden">
    <div class="section">
      <div class="section-label">Выберите дату</div>
      <div class="dates-scroll" id="dates-list"></div>
    </div>
    <div class="section">
      <div class="section-label" id="slots-title">Свободное время</div>
      <div class="slots-grid" id="slots-list">
        <div style="grid-column:1/-1;text-align:center;color:var(--text3);padding:16px;font-size:13px">Выберите дату выше</div>
      </div>
    </div>
    <div style="padding:10px 10px 0">
      <button class="btn btn-p" id="btn3" disabled onclick="goS(3)">Далее →</button>
      <button class="btn btn-s" onclick="goS(1)">← Назад</button>
    </div>
  </div>

  <div id="bs3" class="hidden">
    <div class="section">
      <div class="section-label">Ваши данные</div>
      <div class="input-group">
        <label>Имя и фамилия</label>
        <input type="text" id="inp-name" placeholder="Иван Иванов" oninput="chkForm()" autocomplete="name" />
      </div>
      <div class="input-group">
        <label>Номер телефона</label>
        <input type="tel" id="inp-phone" placeholder="+7 (900) 000-00-00" oninput="phoneMask(this);chkForm()" autocomplete="tel" />
      </div>
    </div>
    <div class="section">
      <div class="section-label">Подтверждение записи</div>
      <div class="summary-box">
        <div class="srow">
          <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Врач</span>
          <span class="val" id="sd">—</span>
        </div>
        <div class="srow">
          <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg>Дата</span>
          <span class="val" id="sdt">—</span>
        </div>
        <div class="srow">
          <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Время</span>
          <span class="val" id="st">—</span>
        </div>
        <div class="srow">
          <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>Адрес</span>
          <span class="val">Профсоюзная, 87</span>
        </div>
      </div>
      <button class="btn btn-p" id="btn-book" disabled onclick="doBook()">Записаться на приём</button>
      <button class="btn btn-s" onclick="goS(2)">← Назад</button>
    </div>
  </div>

  <div id="bs-ok" class="hidden">
    <div class="section success-screen">
      <span class="success-icon">🎉</span>
      <div class="success-title">Вы записаны!</div>
      <div class="success-sub">Ждём вас в клинике «Доктор Смайл»</div>
      <div class="success-card">
        <div class="info-row">
          <span class="info-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span>
          <span class="info-label">Врач</span><span class="info-val" id="ok-d">—</span>
        </div>
        <div class="info-row">
          <span class="info-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg></span>
          <span class="info-label">Дата и время</span><span class="info-val" id="ok-dt">—</span>
        </div>
        <div class="info-row">
          <span class="info-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 13.5a2 2 0 0 1 .44-2.14l.95-1.27a2 2 0 0 0 .45-2.11A12.84 12.84 0 0 1 5.83 5.17a2 2 0 0 0-2-1.72h-3a2 2 0 0 0-2 2.18A19.79 19.79 0 0 0 1.9 14.26 19.5 19.5 0 0 0 13.07 21a19.79 19.79 0 0 0 8.63-3.07A2 2 0 0 0 22 16.92z"/></svg></span>
          <span class="info-label">Телефон</span><span class="info-val" id="ok-p">—</span>
        </div>
        <div class="info-row">
          <span class="info-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg></span>
          <span class="info-label">Адрес</span><span class="info-val">Профсоюзная, 87</span>
        </div>
      </div>
      <div class="success-sub">Напоминание придёт за 24 часа и за 2 часа до приёма.</div>
      <div style="margin-top:10px;font-size:13px;color:var(--accent);font-weight:600">+7 (495) 123-45-67</div>
      <button class="btn btn-s" style="margin-top:20px" onclick="resetBook()">Записаться ещё раз</button>
    </div>
  </div>
</div>

<!-- ── ВОПРОСЫ ── -->
<div class="page" id="page-chat">
  <div class="chat-wrap">
    <div class="chat-msgs" id="chat-msgs"></div>
    <div class="chat-bottom">
      <div class="chat-bar">
        <input type="text" id="chat-in" placeholder="Задайте вопрос..." onkeydown="if(event.key==='Enter')sendMsg()" />
        <button class="send-btn" onclick="sendMsg()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
          </svg>
        </button>
      </div>
      <div class="quick-btns" id="qbtns">
        <button class="qbtn" onclick="sq('Какие у вас цены?')">Цены</button>
        <button class="qbtn" onclick="sq('Где вы находитесь?')">Адрес</button>
        <button class="qbtn" onclick="sq('Режим работы')">Часы</button>
        <button class="qbtn" onclick="sq('Какие врачи у вас есть?')">Врачи</button>
        <button class="qbtn" onclick="sq('Как записаться на приём?')">Запись</button>
        <button class="qbtn" onclick="sq('Есть ли детская стоматология?')">Дети</button>
      </div>
    </div>
  </div>
</div>

<!-- ── МОИ ЗАПИСИ ── -->
<div class="page" id="page-my">
  <div id="my-search">
    <div class="section">
      <div class="section-label">Найти свои записи</div>
      <div class="input-group">
        <label>Ваш номер телефона</label>
        <input type="tel" id="my-phone" placeholder="+7 (900) 000-00-00" oninput="phoneMask(this)" onkeydown="if(event.key==='Enter')loadMy()" />
      </div>
      <button class="btn btn-p" onclick="loadMy()">Найти записи</button>
    </div>
    <div class="section" style="margin-top:0">
      <div class="section-label">Информация о клинике</div>
      <div class="info-grid">
        <div class="info-tile">
          <div class="info-tile-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg></div>
          <div class="info-tile-label">Адрес</div>
          <div class="info-tile-val">Профсоюзная, 87</div>
        </div>
        <div class="info-tile">
          <div class="info-tile-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div>
          <div class="info-tile-label">Часы работы</div>
          <div class="info-tile-val">Ежедневно 8–20</div>
        </div>
        <div class="info-tile">
          <div class="info-tile-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 13.5a2 2 0 0 1 .44-2.14l.95-1.27a2 2 0 0 0 .45-2.11A12.84 12.84 0 0 1 5.83 5.17a2 2 0 0 0-2-1.72h-3a2 2 0 0 0-2 2.18A19.79 19.79 0 0 0 1.9 14.26 19.5 19.5 0 0 0 13.07 21a19.79 19.79 0 0 0 8.63-3.07A2 2 0 0 0 22 16.92z"/></svg></div>
          <div class="info-tile-label">Телефон</div>
          <div class="info-tile-val accent">+7 (495) 123-45-67</div>
        </div>
        <div class="info-tile">
          <div class="info-tile-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg></div>
          <div class="info-tile-label">Врачей</div>
          <div class="info-tile-val">3 специалиста</div>
        </div>
      </div>
    </div>
  </div>
  <div id="my-res" class="hidden">
    <div style="padding:10px 10px 0;display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:13px;font-weight:600;color:var(--text2)" id="my-cnt"></span>
      <button onclick="resetMy()" style="background:none;border:none;color:var(--accent);font-size:13px;font-weight:600;cursor:pointer;font-family:inherit">← Назад</button>
    </div>
    <div id="my-list" style="padding:0 10px"></div>
  </div>
</div>

<!-- ── MODAL О КЛИНИКЕ ── -->
<div class="modal-overlay hidden" id="clinic-modal" onclick="closeClinicModal(event)">
  <div class="modal-sheet" style="max-height:92vh;overflow-y:auto">
    <div class="modal-handle"></div>
    <div style="text-align:center;margin-bottom:20px">
      <div class="clinic-logo-display">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3C9.5 3 7.5 4.5 6.5 7C5.8 5.8 4.6 5 3 5C1.3 7 2.1 10 3.5 12C4.3 13.3 5 14.5 5 17C5 19.5 6 21 7.5 21C9 21 9.5 19 10 17C10.5 15 11 14 12 14C13 14 13.5 15 14 17C14.5 19 15 21 16.5 21C18 21 19 19.5 19 17C19 14.5 19.7 13.3 20.5 12C21.9 10 22.7 7 21 5C19.4 5 18.2 5.8 17.5 7C16.5 4.5 14.5 3 12 3Z"/>
        </svg>
      </div>
      <div style="font-size:20px;font-weight:800;margin-top:14px;color:var(--text)">Доктор Смайл</div>
      <div style="font-size:13px;color:var(--text2);margin-top:4px">Стоматологическая клиника · Москва</div>
    </div>

    <div class="clinic-stats-row">
      <div class="clinic-stat"><div class="clinic-stat-num">2018</div><div class="clinic-stat-lbl">основана</div></div>
      <div class="clinic-stat"><div class="clinic-stat-num">3</div><div class="clinic-stat-lbl">специалиста</div></div>
      <div class="clinic-stat"><div class="clinic-stat-num">5000+</div><div class="clinic-stat-lbl">пациентов</div></div>
    </div>

    <div class="clinic-features">
      <div class="clinic-feature">
        <div class="clinic-feature-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
        </div>
        <div>
          <div class="clinic-feature-title">Гарантия качества</div>
          <div class="clinic-feature-desc">Гарантия на все виды работ. Бесплатные консультации при повторном обращении в течение года.</div>
        </div>
      </div>
      <div class="clinic-feature">
        <div class="clinic-feature-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">
            <circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/>
          </svg>
        </div>
        <div>
          <div class="clinic-feature-title">Современное оборудование</div>
          <div class="clinic-feature-desc">Цифровой рентген, 3D-томография, лазерное лечение кариеса без боли и бормашины.</div>
        </div>
      </div>
      <div class="clinic-feature">
        <div class="clinic-feature-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">
            <path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>
          </svg>
        </div>
        <div>
          <div class="clinic-feature-title">Философия клиники</div>
          <div class="clinic-feature-desc">Безболезненное лечение, максимальный комфорт пациента и долгосрочный предсказуемый результат.</div>
        </div>
      </div>
      <div class="clinic-feature">
        <div class="clinic-feature-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round">
            <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>
          </svg>
        </div>
        <div>
          <div class="clinic-feature-title">Достижения</div>
          <div class="clinic-feature-desc">Рейтинг 4.9 на Яндекс.Картах. Победитель городского конкурса «Лучшая стоматология 2023».</div>
        </div>
      </div>
    </div>

    <div class="modal-info">
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 13.5a2 2 0 0 1 .44-2.14l.95-1.27a2 2 0 0 0 .45-2.11A12.84 12.84 0 0 1 5.83 5.17a2 2 0 0 0-2-1.72h-3a2 2 0 0 0-2 2.18A19.79 19.79 0 0 0 1.9 14.26 19.5 19.5 0 0 0 13.07 21a19.79 19.79 0 0 0 8.63-3.07A2 2 0 0 0 22 16.92z"/></svg>Телефон</span>
        <span class="val" style="color:var(--accent)">+7 (495) 123-45-67</span>
      </div>
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>Адрес</span>
        <span class="val">ул. Профсоюзная, 87</span>
      </div>
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Режим работы</span>
        <span class="val">Ежедневно 8:00–20:00</span>
      </div>
    </div>

    <button class="btn btn-close" onclick="closeClinicModal()">Закрыть</button>
  </div>
</div>

<!-- ── MODAL управление записью ── -->
<div class="modal-overlay hidden" id="booking-modal" onclick="closeModal(event)">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title">Управление записью</div>
    <div class="modal-sub">Что вы хотите сделать с этой записью?</div>
    <div class="modal-info">
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Врач</span>
        <span class="val" id="modal-doc">—</span>
      </div>
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg>Дата и время</span>
        <span class="val" id="modal-dt">—</span>
      </div>
    </div>
    <button class="btn btn-warn" onclick="modalAction('reschedule')">Перенести запись</button>
    <button class="btn btn-danger" onclick="modalAction('cancel')">Отменить запись</button>
    <button class="btn btn-close" onclick="closeModal()">← Назад</button>
  </div>
</div>

<!-- ── MODAL подтверждение отмены ── -->
<div class="modal-overlay hidden" id="cancel-modal" onclick="closeCancelModal(event)">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title">Отменить запись?</div>
    <div class="modal-sub">Вы уверены? Это действие нельзя отменить.</div>
    <div class="modal-info">
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Врач</span>
        <span class="val" id="cancel-doc">—</span>
      </div>
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg>Дата и время</span>
        <span class="val" id="cancel-dt">—</span>
      </div>
    </div>
    <button class="btn btn-danger" id="cancel-confirm-btn" onclick="doCancel()">Да, отменить запись</button>
    <button class="btn btn-close" onclick="closeCancelModal()" style="margin-top:8px">← Назад</button>
  </div>
</div>

<!-- ── MODAL перенос записи ── -->
<div class="modal-overlay hidden" id="reschedule-modal" onclick="closeRescheduleModal(event)">
  <div class="modal-sheet" style="max-height:90vh;overflow-y:auto">
    <div class="modal-handle"></div>
    <div class="modal-title">Перенести запись</div>
    <div class="modal-sub">Выберите новую дату и время</div>
    <div class="modal-info" style="margin-bottom:14px">
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>Врач</span>
        <span class="val" id="rs-doc">—</span>
      </div>
      <div class="srow">
        <span class="lbl"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Было</span>
        <span class="val" id="rs-old-dt">—</span>
      </div>
    </div>
    <div style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px">Новая дата</div>
    <div class="dates-scroll" id="rs-dates" style="margin-bottom:14px"></div>
    <div style="font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:0.8px;margin-bottom:10px">Новое время</div>
    <div class="slots-grid" id="rs-slots" style="margin-bottom:16px">
      <div style="grid-column:1/-1;text-align:center;color:var(--text3);padding:12px;font-size:13px">Выберите дату выше</div>
    </div>
    <button class="btn btn-warn" id="rs-confirm-btn" disabled onclick="doReschedule()">Подтвердить перенос</button>
    <button class="btn btn-close" onclick="closeRescheduleModal()" style="margin-top:8px">← Назад</button>
  </div>
</div>

<script>
vkBridge.send("VKWebAppInit");
const API = '';
let doctors=[], slots=[], selDoc=null, selDateStr=null, selSlot=null;

/* ── Icon strings for JS templates ── */
const ICO = {
  doc: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
  cal: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/></svg>`,
};

/* ── Chat ── */
function addMsg(text, isUser) {
  const el = document.createElement('div');
  el.className = `msg ${isUser ? 'msg-user' : 'msg-bot'}`;
  el.textContent = text;
  const msgs = document.getElementById('chat-msgs');
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'msg msg-bot'; el.id = 'typing';
  el.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  const msgs = document.getElementById('chat-msgs');
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
}

async function sendMsg() {
  const inp = document.getElementById('chat-in');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  addMsg(text, true);
  showTyping();
  try {
    const r = await fetch(`${API}/api/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
    const data = await r.json();
    document.getElementById('typing')?.remove();
    addMsg(data.reply || 'Не удалось получить ответ', false);
  } catch(e) {
    document.getElementById('typing')?.remove();
    addMsg('Ошибка соединения с сервером', false);
  }
}

function sq(text) { document.getElementById('chat-in').value = text; sendMsg(); }

function initChat() {
  const msgs = document.getElementById('chat-msgs');
  if (!msgs.children.length)
    setTimeout(() => addMsg('Здравствуйте! Я ассистент клиники «Доктор Смайл». Задайте любой вопрос о ценах, врачах или услугах — отвечу сразу.', false), 350);
}

/* ── Init ── */
async function init() {
  try {
    doctors = (await (await fetch(`${API}/api/doctors`)).json()).doctors;
    renderDocs();
  } catch(e) {
    document.getElementById('docs-list').innerHTML =
      '<div style="text-align:center;padding:20px;color:var(--danger);font-size:13px">Не удалось загрузить врачей</div>';
  }
  try {
    slots = (await (await fetch(`${API}/api/slots`)).json()).slots;
    renderDates();
  } catch(e) {}
}

function renderDocs() {
  document.getElementById('docs-list').innerHTML = doctors.map(d =>
    `<div class="doctor-card" id="doc${d.id}" onclick="pickDoc(${d.id})">
      <div class="avatar">${d.initials}</div>
      <div style="flex:1"><div class="doc-name">${d.name}</div><div class="doc-spec">${d.spec}</div></div>
      <div class="check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
    </div>`).join('');
}

function pickDoc(id) {
  selDoc = doctors.find(d => d.id === id);
  document.querySelectorAll('.doctor-card').forEach(e => e.classList.remove('selected'));
  document.getElementById(`doc${id}`).classList.add('selected');
  document.getElementById('btn2').disabled = false;
}

function renderDates() {
  const dates = [...new Set(slots.map(s => s.date))];
  document.getElementById('dates-list').innerHTML = dates.map(date => {
    const s = slots.find(x => x.date === date);
    const p = date.split('.');
    return `<div class="date-chip" id="dc${date.replace(/\./g,'')}" onclick="pickDate('${date}')">
      <div class="wday">${s.weekday}</div><div class="dnum">${p[0]}</div><div class="mon">${p[1]}</div>
    </div>`;
  }).join('');
}

function pickDate(date) {
  selDateStr = date; selSlot = null;
  document.getElementById('btn3').disabled = true;
  document.querySelectorAll('.date-chip').forEach(e => e.classList.remove('selected'));
  document.getElementById(`dc${date.replace(/\./g,'')}`).classList.add('selected');
  const daySlots = slots.filter(s => s.date === date);
  document.getElementById('slots-title').textContent = `Свободное время — ${date.slice(0,5)}`;
  const freeSlots = daySlots.filter(s => s.available);
  document.getElementById('slots-list').innerHTML = freeSlots.length
    ? freeSlots.map(s =>
        `<div class="slot" id="sl${s.datetime.replace(/[ :]/g,'')}" onclick="pickSlot('${s.datetime}','${s.time}')">${s.time}</div>`).join('')
    : `<div style="grid-column:1/-1;text-align:center;color:var(--text3);padding:16px;font-size:13px">На этот день всё занято</div>`;
}

function pickSlot(dt, time) {
  selSlot = { datetime: dt, time };
  document.querySelectorAll('.slot').forEach(e => e.classList.remove('selected'));
  document.getElementById(`sl${dt.replace(/[ :]/g,'')}`).classList.add('selected');
  document.getElementById('btn3').disabled = false;
}

function goS(n) {
  ['bs1','bs2','bs3','bs-ok'].forEach(id => document.getElementById(id).classList.add('hidden'));
  if (n <= 3) document.getElementById(`bs${n}`).classList.remove('hidden');
  if (n === 3) {
    document.getElementById('sd').textContent  = selDoc?.name || '—';
    document.getElementById('sdt').textContent = selDateStr || '—';
    document.getElementById('st').textContent  = selSlot?.time || '—';
  }
  window.scrollTo(0, 0);
}

function chkForm() {
  const n = document.getElementById('inp-name').value.trim();
  const p = rawPhone('inp-phone');
  document.getElementById('btn-book').disabled = !(n.length > 2 && p.length >= 10);
}

async function doBook() {
  const name  = document.getElementById('inp-name').value.trim();
  const phone = rawPhone('inp-phone');
  const btn   = document.getElementById('btn-book');
  btn.disabled = true; btn.textContent = 'Записываем...';
  try {
    const r = await fetch(`${API}/api/book`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doctor_id: selDoc.id, datetime: selSlot.datetime, name, phone })
    });
    const data = await r.json();
    if (r.ok) {
      document.getElementById('ok-d').textContent  = data.booking.doctor;
      document.getElementById('ok-dt').textContent = data.booking.datetime;
      document.getElementById('ok-p').textContent  = data.booking.phone;
      ['bs1','bs2','bs3'].forEach(id => document.getElementById(id).classList.add('hidden'));
      document.getElementById('bs-ok').classList.remove('hidden');
      window.scrollTo(0, 0);
    } else {
      alert(data.detail || 'Ошибка записи');
      btn.disabled = false; btn.textContent = 'Записаться на приём';
    }
  } catch(e) {
    alert('Ошибка соединения с сервером');
    btn.disabled = false; btn.textContent = 'Записаться на приём';
  }
}

function resetBook() {
  selDoc = null; selDateStr = null; selSlot = null;
  document.getElementById('inp-name').value  = '';
  document.getElementById('inp-phone').value = '';
  document.querySelectorAll('.doctor-card').forEach(e => e.classList.remove('selected'));
  document.getElementById('btn2').disabled = true;
  ['bs2','bs3','bs-ok'].forEach(id => document.getElementById(id).classList.add('hidden'));
  document.getElementById('bs1').classList.remove('hidden');
  init();
}

/* ── My bookings ── */
async function loadMy() {
  const phone = document.getElementById('my-phone').value.trim().replace(/\D/g,'');
  if (phone.length < 7) { alert('Введите номер телефона'); return; }
  const btn = event.target;
  btn.textContent = 'Ищем...'; btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/my_bookings?phone=${phone}`);
    if (!r.ok) showMyEmpty();
    else { const data = await r.json(); showMyRes(data.bookings); }
  } catch(e) { showMyEmpty(); }
  btn.textContent = 'Найти записи'; btn.disabled = false;
}

let currentBookings = [], activeBookingIdx = null;

function showMyRes(bookings) {
  currentBookings = bookings || [];
  document.getElementById('my-search').classList.add('hidden');
  document.getElementById('my-res').classList.remove('hidden');
  const count = currentBookings.length;
  document.getElementById('my-cnt').textContent = count ? `Найдено записей: ${count}` : 'Записей не найдено';
  if (!count) {
    document.getElementById('my-list').innerHTML =
      `<div class="empty"><span class="ei"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M9 7h6M9 11h6M9 15h4"/></svg></span><p>Записей по этому номеру не найдено</p></div>`;
    return;
  }
  const sc = { 'Подтверждено':'st-ok','Отменено':'st-no','Перенесено':'st-mv' };
  const si = { 'Подтверждено':'Подтверждено','Отменено':'Отменено','Перенесено':'Перенесено' };
  document.getElementById('my-list').innerHTML = currentBookings.map((b, i) => {
    const canManage = b.status === 'Подтверждено' || b.status === 'Перенесено';
    return `<div class="bcard" onclick="${canManage ? `openModal(${i})` : ''}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span class="bstatus ${sc[b.status]||'st-ok'}">${si[b.status]||b.status}</span>
        ${canManage ? '<span style="font-size:12px;color:var(--text3)">Нажмите для управления →</span>' : ''}
      </div>
      <div class="srow"><span class="lbl">${ICO.doc}Врач</span><span class="val">${b.doctor}</span></div>
      <div class="srow"><span class="lbl">${ICO.cal}Дата и время</span><span class="val">${b.datetime}</span></div>
    </div>`;
  }).join('');
}

/* ── Booking modal ── */
function openModal(idx) {
  activeBookingIdx = idx;
  const b = currentBookings[idx];
  document.getElementById('modal-doc').textContent = b.doctor;
  document.getElementById('modal-dt').textContent  = b.datetime;
  document.querySelector('#booking-modal .modal-sub').textContent = 'Что вы хотите сделать с этой записью?';
  document.getElementById('booking-modal').classList.remove('hidden');
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('booking-modal'))
    document.getElementById('booking-modal').classList.add('hidden');
}

/* ── О клинике modal ── */
function openClinicModal() {
  document.getElementById('clinic-modal').classList.remove('hidden');
}
function closeClinicModal(e) {
  if (!e || e.target === document.getElementById('clinic-modal'))
    document.getElementById('clinic-modal').classList.add('hidden');
}

/* ── Reschedule ── */
let rsSlots = [], rsSelDate = null, rsSelSlot = null;

async function modalAction(action) {
  const b = currentBookings[activeBookingIdx];
  document.getElementById('booking-modal').classList.add('hidden');

  if (action === 'cancel') {
    document.getElementById('cancel-doc').textContent = b.doctor;
    document.getElementById('cancel-dt').textContent  = b.datetime;
    document.getElementById('cancel-modal').classList.remove('hidden');
  } else {
    if (!b.can_modify) {
      document.querySelector('#booking-modal .modal-sub').innerHTML =
        'Перенести можно не позднее чем за 24 ч до приёма.<br>Позвоните нам:<br><br>' +
        `<a href="tel:+74951234567" style="font-size:18px;font-weight:700;color:var(--accent);text-decoration:none">+7 (495) 123-45-67</a>`;
      document.getElementById('booking-modal').classList.remove('hidden');
      return;
    }
    document.getElementById('rs-doc').textContent    = b.doctor;
    document.getElementById('rs-old-dt').textContent = b.datetime;
    document.getElementById('rs-confirm-btn').disabled = true;
    rsSelDate = null; rsSelSlot = null; rsSlots = [];
    document.getElementById('rs-dates').innerHTML =
      '<div style="color:var(--text3);padding:12px 4px;font-size:13px">Загружаем даты...</div>';
    document.getElementById('rs-slots').innerHTML =
      '<div style="grid-column:1/-1;text-align:center;color:var(--text3);padding:12px;font-size:13px">Выберите дату выше</div>';
    document.getElementById('reschedule-modal').classList.remove('hidden');
    try {
      const data = await (await fetch(`${API}/api/slots`)).json();
      rsSlots = (data.slots || []).filter(s => s.available);
    } catch(e) { rsSlots = []; }
    renderRsDates();
  }
}

function renderRsDates() {
  const dates = [...new Set(rsSlots.map(s => s.date))];
  if (!dates.length) {
    document.getElementById('rs-dates').innerHTML =
      '<div style="color:var(--text3);padding:12px 4px;font-size:13px">Нет доступных дат</div>';
    return;
  }
  document.getElementById('rs-dates').innerHTML = dates.map(date => {
    const s = rsSlots.find(x => x.date === date);
    const p = date.split('.');
    return `<div class="date-chip" id="rsdc${date.replace(/\./g,'')}" onclick="rsPickDate('${date}')">
      <div class="wday">${s.weekday}</div><div class="dnum">${p[0]}</div><div class="mon">${p[1]}</div>
    </div>`;
  }).join('');
}

function rsPickDate(date) {
  rsSelDate = date; rsSelSlot = null;
  document.getElementById('rs-confirm-btn').disabled = true;
  document.querySelectorAll('#rs-dates .date-chip').forEach(e => e.classList.remove('selected'));
  document.getElementById(`rsdc${date.replace(/\./g,'')}`).classList.add('selected');
  const daySlots = rsSlots.filter(s => s.date === date);
  document.getElementById('rs-slots').innerHTML = daySlots.length
    ? daySlots.map(s =>
        `<div class="slot" id="rssl${s.datetime.replace(/[ :]/g,'')}" onclick="rsPickSlot('${s.datetime}','${s.time}')">${s.time}</div>`).join('')
    : '<div style="grid-column:1/-1;text-align:center;color:var(--text3);padding:12px;font-size:13px">На этот день нет свободных окон</div>';
}

function rsPickSlot(dt, time) {
  rsSelSlot = { datetime: dt, time };
  document.querySelectorAll('#rs-slots .slot').forEach(e => e.classList.remove('selected'));
  document.getElementById(`rssl${dt.replace(/[ :]/g,'')}`).classList.add('selected');
  document.getElementById('rs-confirm-btn').disabled = false;
}

async function doReschedule() {
  if (!rsSelSlot) return;
  const b = currentBookings[activeBookingIdx];
  const phone = rawPhone('my-phone');
  const btn = document.getElementById('rs-confirm-btn');
  btn.disabled = true; btn.textContent = 'Переносим...';
  try {
    const r = await fetch(`${API}/api/reschedule_booking`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, doctor: b.doctor, old_datetime: b.datetime, new_datetime: rsSelSlot.datetime })
    });
    if (r.ok) {
      document.getElementById('reschedule-modal').classList.add('hidden');
      showToast('Запись перенесена');
      loadMy();
    } else {
      const d = await r.json();
      showToast(d.detail || 'Ошибка переноса', true);
      btn.disabled = false; btn.textContent = 'Подтвердить перенос';
    }
  } catch(e) {
    showToast('Ошибка соединения', true);
    btn.disabled = false; btn.textContent = 'Подтвердить перенос';
  }
}

async function doCancel() {
  const b = currentBookings[activeBookingIdx];
  const phone = rawPhone('my-phone');
  const btn = document.getElementById('cancel-confirm-btn');
  btn.disabled = true; btn.textContent = 'Отменяем...';
  try {
    const r = await fetch(`${API}/api/cancel_booking`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, doctor: b.doctor, datetime: b.datetime })
    });
    if (r.ok) {
      document.getElementById('cancel-modal').classList.add('hidden');
      showToast('Запись отменена');
      loadMy();
    } else {
      const d = await r.json();
      showToast(d.detail || 'Ошибка отмены', true);
      btn.disabled = false; btn.textContent = 'Да, отменить запись';
    }
  } catch(e) {
    showToast('Ошибка соединения', true);
    btn.disabled = false; btn.textContent = 'Да, отменить запись';
  }
}

function closeCancelModal(e) {
  if (!e || e.target === document.getElementById('cancel-modal')) {
    document.getElementById('cancel-modal').classList.add('hidden');
    document.getElementById('booking-modal').classList.remove('hidden');
  }
}

function closeRescheduleModal(e) {
  if (!e || e.target === document.getElementById('reschedule-modal')) {
    document.getElementById('reschedule-modal').classList.add('hidden');
    document.getElementById('booking-modal').classList.remove('hidden');
  }
}

function showToast(msg, isErr = false) {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText =
    `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);` +
    `background:${isErr ? 'var(--danger)' : 'var(--success)'};` +
    `color:white;padding:12px 22px;border-radius:12px;font-size:14px;font-weight:600;` +
    `z-index:999;box-shadow:0 4px 24px rgba(0,0,0,0.45);` +
    `backdrop-filter:blur(20px);white-space:nowrap;` +
    `animation:fadeUp 0.35s cubic-bezier(0.16,1,0.3,1) both`;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

function showMyEmpty() {
  document.getElementById('my-search').classList.add('hidden');
  document.getElementById('my-res').classList.remove('hidden');
  document.getElementById('my-cnt').textContent = '';
  document.getElementById('my-list').innerHTML =
    `<div class="empty"><span class="ei"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M9 7h6M9 11h6M9 15h4"/></svg></span><p>Записей не найдено</p></div>`;
}

function resetMy() {
  document.getElementById('my-res').classList.add('hidden');
  document.getElementById('my-search').classList.remove('hidden');
  document.getElementById('my-phone').value = '';
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(e => e.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(e => e.classList.remove('active'));
  document.getElementById(`page-${name}`).classList.add('active');
  document.getElementById(`nav-${name}`).classList.add('active');
  if (name === 'chat') initChat();
  window.scrollTo(0, 0);
}

function phoneMask(input) {
  let digits = input.value.replace(/\D/g, '');
  if (digits.startsWith('8')) digits = '7' + digits.slice(1);
  if (digits.startsWith('7')) digits = digits.slice(1);
  digits = digits.slice(0, 10);
  let result = '';
  if (digits.length > 0) result = '+7 (' + digits.slice(0, 3);
  if (digits.length >= 3) result += ') ' + digits.slice(3, 6);
  if (digits.length >= 6) result += '-' + digits.slice(6, 8);
  if (digits.length >= 8) result += '-' + digits.slice(8, 10);
  input.value = result;
}

function rawPhone(inputId) {
  return document.getElementById(inputId).value.replace(/\D/g, '');
}

init();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def root():
    return _HTML


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
def book(req: BookingRequest):
    try:
        doctor = next((d for d in DOCTORS if d["id"] == req.doctor_id), None)
        if not doctor:
            raise HTTPException(status_code=400, detail="Врач не найден")

        busy = get_busy_slots()
        dt_obj = datetime.strptime(req.datetime, "%Y-%m-%d %H:%M")
        key = dt_obj.strftime("%Y-%m-%d %H:%M")
        if key in busy:
            raise HTTPException(status_code=409, detail="Это время уже занято")

        name_parts = req.name.strip().split(maxsplit=1)
        first_name = name_parts[0] if name_parts else req.name
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        sheet = get_sheet()
        sheet.append_row([
            first_name,
            last_name,
            req.phone,
            doctor["name"],
            dt_obj.strftime("%d.%m.%Y %H:%M"),
            "Подтверждено",
            ""
        ])

        return {
            "success": True,
            "booking": {
                "name": req.name,
                "phone": req.phone,
                "doctor": doctor["name"],
                "datetime": dt_obj.strftime("%d.%m.%Y в %H:%M")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/my_bookings")
def my_bookings(phone: str):
    try:
        sheet = get_sheet()
        all_values = sheet.get_all_values()
        if not all_values:
            return {"bookings": []}

        headers = [h.strip() for h in all_values[0]]
        phone_clean = re.sub(r'\D', '', phone)

        try:
            idx_phone = headers.index("Телефон")
            idx_doctor = headers.index("Врач")
            idx_dt = headers.index("Дата и время")
            idx_status = headers.index("Статус")
        except ValueError:
            idx_phone, idx_doctor, idx_dt, idx_status = 2, 3, 4, 5

        result = []
        for row in all_values[1:]:
            while len(row) <= max(idx_phone, idx_doctor, idx_dt, idx_status):
                row.append("")
            row_phone = re.sub(r'\D', '', row[idx_phone])
            if row_phone == phone_clean:
                result.append({
                    "doctor": row[idx_doctor],
                    "datetime": row[idx_dt],
                    "status": row[idx_status]
                })
        return {"bookings": result}
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
                        {"role": "user", "content": req.message}
                    ],
                    "max_tokens": 300
                },
                timeout=15
            )
            data = r.json()
            return {"reply": data["choices"][0]["message"]["content"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Debug (убери после проверки) ────────────────────────────────────────────

@app.get("/api/debug")
def debug():
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    busy = get_busy_slots()
    return {
        "headers": all_values[0] if all_values else [],
        "rows": all_values[1:],
        "busy_slots": sorted(list(busy))
    }
