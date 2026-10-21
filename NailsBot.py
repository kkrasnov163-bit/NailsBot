"""
Телеграм-бот записи для маникюр-салона. БЕЗ внешних библиотек — только стандартный Python 3.8+.
Запуск:  python bot.py
"""
import calendar as pycal
import csv
import html
import io
import json
import logging
import sqlite3
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# ───────────────────────── НАСТРОЙКИ (впишите свои) ─────────────────────────
BOT_TOKEN = "8932765028:AAEPstLQ25ofNNL7d6Es4jO0PXumuuI1WVo"       # токен от @BotFather
ADMIN_IDS = {1057772062}            # ваш ID от @userinfobot (несколько: {111, 222})
SALON_NAME = "Nail Studio"
MAX_ACTIVE_PER_USER = 3              # макс. активных записей у одного клиента
# Услуги и цены: (название, цена). Цена — любой текст, например "1500 ₽" или "€35".
SERVICES = [
    ("Маникюр + гель-лак", "2000 ₽"),
    ("Педикюр",            "2500 ₽"),
    ("Наращивание ногтей", "3500 ₽"),
    ("Снятие покрытия",    "500 ₽"),
    ("Дизайн (1 ноготь)",  "100 ₽"),
]
DEFAULTS = {"work_start": "10:00", "work_end": "19:00", "slot_min": "60", "days_off": "6"}  # 6 = воскресенье
# ────────────────────────────────────────────────────────────────────────────

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
          "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
LEGEND = "🟢 есть время   🔴 занято   ⚫ закрыто"
EMOJI = {"free": "🟢", "full": "🔴", "closed": "⚫"}
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ───────────────────────── Telegram API ─────────────────────────
def api(method, **params):
    req = urllib.request.Request(f"{API}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        logging.warning("%s: %s", method, e.read().decode()[:200])
    except Exception as e:
        logging.warning("%s: %s", method, e)
    return None


def send(chat_id, text, kb=None):
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)


def send_document(chat_id, filename, content: bytes):
    b = "----salonbotboundary"
    body = (f'--{b}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f'Content-Type: text/csv\r\n\r\n').encode() + content + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"{API}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        logging.warning("sendDocument: %s", e)


def notify_admins(text, kb=None):
    for aid in ADMIN_IDS:
        send(aid, text, kb)


# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect("salon.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS blocked(date TEXT, time TEXT, PRIMARY KEY(date, time)); -- time='' = весь день
CREATE TABLE IF NOT EXISTS bookings(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, name TEXT, phone TEXT,
    date TEXT, time TEXT, status TEXT DEFAULT 'active', created_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active ON bookings(date, time) WHERE status='active';
""")
for k, v in DEFAULTS.items():
    db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
for _col in ("service", "price"):
    if _col not in [r[1] for r in db.execute("PRAGMA table_info(bookings)")]:
        db.execute(f"ALTER TABLE bookings ADD COLUMN {_col} TEXT")
db.commit()


def get(key):
    return db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()["value"]


def put(key, value):
    db.execute("UPDATE settings SET value=? WHERE key=?", (str(value), key))
    db.commit()


def days_off():
    return {int(x) for x in get("days_off").split(",") if x != ""}


def slot_times():
    t = datetime.strptime(get("work_start"), "%H:%M")
    end = datetime.strptime(get("work_end"), "%H:%M")
    step = timedelta(minutes=int(get("slot_min")))
    out = []
    while t + step <= end:
        out.append(t.strftime("%H:%M"))
        t += step
    return out


def slot_table(d, admin=False):
    """[(время, 'free'|'busy'|'closed', запись|None)]"""
    ds = d.isoformat()
    bk = {r["time"]: r for r in db.execute("SELECT * FROM bookings WHERE date=? AND status='active'", (ds,))}
    blk = {r["time"] for r in db.execute("SELECT time FROM blocked WHERE date=?", (ds,))}
    day_closed = "" in blk or d.weekday() in days_off()
    now = datetime.now()
    rows = []
    for t in sorted(set(slot_times()) | set(bk)):
        if t in bk:
            st = "busy"
        elif day_closed or t in blk:
            st = "closed"
        elif not admin and datetime.combine(d, datetime.strptime(t, "%H:%M").time()) <= now:
            st = "closed"
        else:
            st = "free"
        rows.append((t, st, bk.get(t)))
    return rows


def day_state(d, admin=False):
    if d < date.today() and not admin:
        return "closed"
    sts = [s for _, s, _ in slot_table(d, admin)]
    if "free" in sts:
        return "free"
    return "full" if "busy" in sts else "closed"


# ───────────────────────── Клавиатуры ─────────────────────────
def esc(s):
    return html.escape(str(s or ""))


def fmt(d):
    return f"{d.strftime('%d.%m.%Y')} ({WD[d.weekday()]})"


def btn(text, data=None, url=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = data
    return b


def kbd(rows):
    return {"inline_keyboard": rows}


def chunk(items, n=3):
    return [items[i:i + n] for i in range(0, len(items), n)]


MAIN_KB = {"keyboard": [[{"text": "💅 Прайс-лист"}],
                        [{"text": "📅 Записаться"}, {"text": "📋 Мои записи"}]], "resize_keyboard": True}


choice = {}   # user_id -> индекс выбранной услуги


def services_text():
    return "<b>Услуги и цены</b>\n" + "\n".join(f"• {esc(n)} — {esc(p)}" for n, p in SERVICES)


def services_kb():
    return kbd([[btn(f"{n} — {p}", f"svc:{i}")] for i, (n, p) in enumerate(SERVICES)])


def cal_text(uid):
    i = choice.get(uid)
    head = f"Услуга: <b>{esc(SERVICES[i][0])} — {esc(SERVICES[i][1])}</b>\n" if i is not None else ""
    return head + f"Выберите дату:\n{LEGEND}"


def svc_label(r):
    return f" — {r['service']} ({r['price']})" if r["service"] else ""


def calendar_kb(offset, prefix=""):
    admin = prefix == "a"
    today = date.today()
    y, m = today.year, today.month + offset
    if m > 12:
        y, m = y + 1, m - 12
    rows = [[btn(f"{MONTHS[m - 1]} {y}", "noop")], [btn(w, "noop") for w in WD]]
    for week in pycal.monthcalendar(y, m):
        row = []
        for n in week:
            if n == 0:
                row.append(btn(" ", "noop"))
                continue
            d = date(y, m, n)
            st = day_state(d, admin)
            if d < today and not admin:
                row.append(btn("·", "noop"))
            elif st == "closed" and not admin:
                row.append(btn(f"{n}⚫", "closed"))
            elif st == "full" and not admin:
                row.append(btn(f"{n}🔴", "full"))
            else:
                row.append(btn(f"{n}{EMOJI[st]}", f"{prefix}day:{d.isoformat()}"))
        rows.append(row)
    rows.append([btn("Следующий месяц ▶", f"{prefix}cal:1")] if offset == 0
                else [btn("◀ Текущий месяц", f"{prefix}cal:0")])
    if admin:
        rows.append([btn("⬅ В админ-панель", "apanel")])
    return kbd(rows)


PANEL_KB = kbd([
    [btn("📅 Расписание / записи", "acal:0")],
    [btn("⚙️ Настройки графика", "aset")],
    [btn("📤 Экспорт (Excel/CSV)", "aexport"), btn("📢 Рассылка", "abroad")],
])


def admin_day_view(d):
    ds = d.isoformat()
    lines, btns = [fmt(d)], []
    for t, st, bk in slot_table(d, admin=True):
        ct = t.replace(":", "")
        if st == "busy":
            lines.append(f"{t} 🔴 {bk['name']} {bk['phone']} [{bk['service'] or '-'}]")
            btns.append(btn(f"🔴 {t}", f"abk:{bk['id']}"))
        elif st == "free":
            lines.append(f"{t} 🟢 свободно")
            btns.append(btn(f"🟢 {t}", f"ablock:{ds}:{ct}"))
        else:
            lines.append(f"{t} ⚫ закрыто")
            btns.append(btn(f"⚫ {t}", f"ablock:{ds}:{ct}"))
    whole = db.execute("SELECT 1 FROM blocked WHERE date=? AND time=''", (ds,)).fetchone()
    rows = chunk(btns) + [[btn("✅ Открыть день" if whole else "🚫 Закрыть весь день", f"adayblock:{ds}")],
                          [btn("⬅ К календарю", "acal:0")]]
    note = "\n\nНажмите на 🔴 — карточка записи, на 🟢/⚫ — закрыть/открыть время."
    if d.weekday() in days_off():
        note += "\n(выходной по графику — меняется в настройках)"
    return f"<pre>{esc(chr(10).join(lines))}</pre>{note}", kbd(rows)


def settings_view():
    off = days_off()
    text = (f"<b>Настройки графика</b>\nЧасы работы: {get('work_start')}–{get('work_end')}\n"
            f"Длительность записи: {get('slot_min')} мин\n"
            "Нажмите на день недели, чтобы сделать его выходным/рабочим.")
    rows = [[btn("🕐 Часы работы", "aset_hours"), btn("⏱ Длительность", "aset_slot")],
            [btn(f"{WD[i]} {'🚫' if i in off else '✅'}", f"adow:{i}") for i in range(7)],
            [btn("⬅ В админ-панель", "apanel")]]
    return text, kbd(rows)


# ───────────────────────── Сообщения ─────────────────────────
states = {}   # user_id -> {"s": состояние, ...данные}


def is_admin(uid):
    return uid in ADMIN_IDS


def on_message(m):
    user, chat = m["from"], m["chat"]["id"]
    uid = user["id"]
    text = (m.get("text") or "").strip()
    st = states.get(uid)

    if text == "/cancel":
        states.pop(uid, None)
        return send(chat, "Действие отменено.", MAIN_KB)
    if text == "/start":
        states.pop(uid, None)
        send(chat, f"Привет! Это бот записи в <b>{esc(SALON_NAME)}</b> 💅\n"
                   "Нажмите «📅 Записаться», чтобы выбрать дату и время.", MAIN_KB)
        # ← дополнительная кнопка «просто выводит список услуг с ценами»
        return send(chat, "💅 <b>Услуги и цены</b>:",
                    kbd([[btn("📋 Показать прайс-лист", "show_prices")]]))
    if text == "/admin" and is_admin(uid):
        states.pop(uid, None)
        return send(chat, "🛠 Админ-панель", PANEL_KB)

    # --- диалоги по шагам
    if st:
        s = st["s"]
        if s == "name" and text:
            st.update(s="phone", name=text[:60])
            return send(chat, "Оставьте номер телефона (кнопкой или текстом):",
                        {"keyboard": [[{"text": "📱 Отправить мой номер", "request_contact": True}]],
                         "resize_keyboard": True, "one_time_keyboard": True})
        if s == "phone":
            phone = (m.get("contact") or {}).get("phone_number") or text
            if sum(c.isdigit() for c in phone) < 6:
                return send(chat, "Не похоже на номер телефона, попробуйте ещё раз.")
            return finish_booking(chat, user, states.pop(uid), phone)
        if is_admin(uid) and text:
            if s == "amsg":
                states.pop(uid)
                r = send(st["uid"], f"💌 Сообщение от салона:\n{esc(text)}")
                return send(chat, "Отправлено ✅" if r else "Не удалось отправить (клиент мог заблокировать бота).")
            if s == "hours":
                try:
                    a, b = [x.strip() for x in text.split("-")]
                    datetime.strptime(a, "%H:%M"); datetime.strptime(b, "%H:%M")
                    assert a < b
                except Exception:
                    return send(chat, "Неверный формат. Пример: 10:00-19:00")
                put("work_start", a); put("work_end", b); states.pop(uid)
                return send(chat, "Сохранено ✅", kbd([[btn("⚙️ К настройкам", "aset")]]))
            if s == "slot":
                if not text.isdigit() or not 15 <= int(text) <= 480:
                    return send(chat, "Введите число от 15 до 480.")
                put("slot_min", int(text)); states.pop(uid)
                return send(chat, "Сохранено ✅ (созданные записи не пропадут)", kbd([[btn("⚙️ К настройкам", "aset")]]))
            if s == "broadcast":
                states.pop(uid)
                ok = 0
                for r in db.execute("SELECT DISTINCT user_id FROM bookings").fetchall():
                    if send(r["user_id"], f"📢 {esc(SALON_NAME)}:\n{esc(text)}"):
                        ok += 1
                    _time.sleep(0.05)
                return send(chat, f"Отправлено: {ok}")
        return

    # --- меню клиента
    if text in ("💅 Прайс-лист", "💰 Услуги и цены", "/prices"):
        return send(chat, services_text(), kbd([[btn("📅 Записаться", "book")]]))
    if text in ("📅 Записаться", "/book"):
        return send(chat, "Выберите услугу:", services_kb())
    if text in ("📋 Мои записи", "/my"):
        rows = db.execute("SELECT * FROM bookings WHERE user_id=? AND status='active' AND date>=? ORDER BY date,time",
                          (uid, date.today().isoformat())).fetchall()
        if not rows:
            return send(chat, "У вас нет активных записей.")
        kb = [[btn(f"❌ Отменить {date.fromisoformat(r['date']).strftime('%d.%m')} {r['time']}", f"cancel:{r['id']}")]
              for r in rows]
        return send(chat, "Ваши записи:\n" + "\n".join(
            f"• {fmt(date.fromisoformat(r['date']))}, {r['time']}{esc(svc_label(r))}" for r in rows), kbd(kb))

    # --- любое другое сообщение клиента уходит администратору
    if text and not text.startswith("/") and not is_admin(uid):
        who = esc(user.get("first_name", "")) + (f" (@{esc(user['username'])})" if user.get("username") else "")
        notify_admins(f"💬 Сообщение от {who}:\n{esc(text)}", kbd([[btn("✉ Ответить", f"amsg:{uid}")]]))
        send(chat, "Сообщение передано администратору, скоро ответим 🙌")


def finish_booking(chat, user, data, phone):
    d = date.fromisoformat(data["date"])
    sname, sprice = SERVICES[data["svc"]]
    try:
        if (data["time"], "free") not in [(x, s) for x, s, _ in slot_table(d)]:
            raise sqlite3.IntegrityError
        db.execute("INSERT INTO bookings(user_id,username,name,phone,date,time,created_at,service,price) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (user["id"], user.get("username"), data["name"], phone, data["date"], data["time"],
                    datetime.now().isoformat(timespec="seconds"), sname, sprice))
        db.commit()
    except sqlite3.IntegrityError:
        return send(chat, "Это время только что заняли 😔 Выберите другое.", MAIN_KB)
    send(chat, f"✅ Вы записаны на <b>{fmt(d)}, {data['time']}</b>\n"
               f"Услуга: {esc(sname)} — {esc(sprice)}\n"
               "Отменить запись можно в разделе «📋 Мои записи».", MAIN_KB)
    notify_admins(f"🆕 Новая запись: <b>{fmt(d)} {data['time']}</b>\n{esc(sname)} — {esc(sprice)}\n"
                  f"{esc(data['name'])}, {esc(phone)}"
                  + (f" (@{esc(user['username'])})" if user.get("username") else ""),
                  kbd([[btn("✉ Написать клиенту", f"amsg:{user['id']}")]]))


# ───────────────────────── Нажатия на кнопки ─────────────────────────
def on_callback(cq):
    alert = None
    try:
        alert = route_callback(cq)
    except Exception:
        logging.exception("callback error")
    api("answerCallbackQuery", callback_query_id=cq["id"], text=alert, show_alert=bool(alert))


def route_callback(cq):
    data, user = cq["data"], cq["from"]
    uid, chat, mid = user["id"], cq["message"]["chat"]["id"], cq["message"]["message_id"]

    def edit(text, kb=None):
        api("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML", reply_markup=kb)

    if data == "noop":
        return
    if data in ("closed", "full", "busy"):
        return {"closed": "В этот день запись закрыта", "full": "На этот день всё занято",
                "busy": "Это время уже занято"}[data]

    # ---------- клиент ----------
    if data == "show_prices":
        return edit(services_text(), kbd([
            [btn("📅 Записаться", "book")],
        ]))
    if data == "book":
        return edit("Выберите услугу:", services_kb())
    if data.startswith("svc:"):
        choice[uid] = int(data[4:])
        return edit(cal_text(uid), calendar_kb(0))
    if data.startswith("cal:"):
        return edit(cal_text(uid), calendar_kb(int(data[4:])))
    if data.startswith("day:"):
        d = date.fromisoformat(data[4:])
        btns = []
        for t, st, _ in slot_table(d):
            if st == "free":
                btns.append(btn(f"🟢 {t}", f"slot:{d.isoformat()}:{t.replace(':', '')}"))
            else:
                btns.append(btn(f"{'🔴' if st == 'busy' else '⚫'} {t}", "busy" if st == "busy" else "closed"))
        return edit(f"<b>{fmt(d)}</b>\nВыберите время:", kbd(chunk(btns) + [[btn("⬅ К календарю", "cal:0")]]))
    if data.startswith("slot:"):
        _, ds, hhmm = data.split(":")
        t, d = f"{hhmm[:2]}:{hhmm[2:]}", date.fromisoformat(ds)
        if choice.get(uid) is None:
            return "Сначала выберите услугу: нажмите «📅 Записаться»"
        if (t, "free") not in [(x, s) for x, s, _ in slot_table(d)]:
            return "Увы, это время уже занято"
        active = db.execute("SELECT COUNT(*) c FROM bookings WHERE user_id=? AND status='active' AND date>=?",
                            (uid, date.today().isoformat())).fetchone()["c"]
        if active >= MAX_ACTIVE_PER_USER:
            return f"Максимум активных записей: {MAX_ACTIVE_PER_USER}. Отмените одну из текущих."
        states[uid] = {"s": "name", "date": ds, "time": t, "svc": choice[uid]}
        send(chat, f"Вы выбрали <b>{fmt(d)}, {t}</b>\n{esc(SERVICES[choice[uid]][0])} — {esc(SERVICES[choice[uid]][1])}\nКак вас зовут? (отмена: /cancel)", {"remove_keyboard": True})
        return
    if data.startswith("cancel:"):
        bid = data.split(":")[1]
        return edit("Точно отменить запись?", kbd([[btn("Да, отменить", f"cancelyes:{bid}"), btn("Нет", "cancelno")]]))
    if data == "cancelno":
        return edit("Хорошо, запись сохранена 👍")
    if data.startswith("cancelyes:"):
        r = db.execute("SELECT * FROM bookings WHERE id=? AND user_id=? AND status='active'",
                       (int(data.split(":")[1]), uid)).fetchone()
        if not r:
            return edit("Запись не найдена или уже отменена.")
        db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (r["id"],))
        db.commit()
        edit("Запись отменена ✅")
        return notify_admins(f"❌ Клиент отменил запись: <b>{fmt(date.fromisoformat(r['date']))} {r['time']}</b>\n"
                             f"{esc(r['name'])}, {esc(r['phone'])}")

    # ---------- админ ----------
    if not data.startswith("a") or not is_admin(uid):
        return
    if data == "apanel":
        return edit("🛠 Админ-панель", PANEL_KB)
    if data.startswith("acal:"):
        return edit(f"Выберите день:\n{LEGEND}", calendar_kb(int(data[5:]), "a"))
    if data.startswith("aday:"):
        return edit(*admin_day_view(date.fromisoformat(data[5:])))
    if data.startswith("ablock:"):
        _, ds, hhmm = data.split(":")
        t = f"{hhmm[:2]}:{hhmm[2:]}"
        if db.execute("SELECT 1 FROM blocked WHERE date=? AND time=?", (ds, t)).fetchone():
            db.execute("DELETE FROM blocked WHERE date=? AND time=?", (ds, t))
        else:
            db.execute("INSERT INTO blocked VALUES(?,?)", (ds, t))
        db.commit()
        return edit(*admin_day_view(date.fromisoformat(ds)))
    if data.startswith("adayblock:"):
        ds = data[10:]
        if db.execute("SELECT 1 FROM blocked WHERE date=? AND time=''", (ds,)).fetchone():
            db.execute("DELETE FROM blocked WHERE date=? AND time=''", (ds,))
        else:
            db.execute("INSERT INTO blocked VALUES(?, '')", (ds,))
        db.commit()
        return edit(*admin_day_view(date.fromisoformat(ds)))
    if data.startswith("abk:"):
        r = db.execute("SELECT * FROM bookings WHERE id=?", (int(data[4:]),)).fetchone()
        if not r:
            return "Запись не найдена"
        text = (f"<b>Запись #{r['id']}</b>\n{fmt(date.fromisoformat(r['date']))}, {r['time']}\n"
                f"Услуга: {esc(r['service'] or '—')} — {esc(r['price'] or '')}\n"
                f"Имя: {esc(r['name'])}\nТелефон: {esc(r['phone'])}\n"
                f"Username: {'@' + esc(r['username']) if r['username'] else '—'}")
        return edit(text, kbd([
            [btn("✉ Написать клиенту", f"amsg:{r['user_id']}"), btn("❌ Отменить запись", f"acancel:{r['id']}")],
            [btn("👤 Профиль в Telegram", url=f"tg://user?id={r['user_id']}")],
            [btn("⬅ Назад", f"aday:{r['date']}")]]))
    if data.startswith("acancel:"):
        r = db.execute("SELECT * FROM bookings WHERE id=? AND status='active'", (int(data[8:]),)).fetchone()
        if not r:
            return "Уже отменена"
        db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (r["id"],))
        db.commit()
        send(r["user_id"], f"К сожалению, ваша запись на {fmt(date.fromisoformat(r['date']))} {r['time']} "
                           "была отменена администратором. Запишитесь на другое время 🙏")
        return edit(*admin_day_view(date.fromisoformat(r["date"])))
    if data.startswith("amsg:"):
        states[uid] = {"s": "amsg", "uid": int(data[5:])}
        return send(chat, "Напишите сообщение клиенту (отмена: /cancel):")
    if data == "aset":
        return edit(*settings_view())
    if data.startswith("adow:"):
        off = days_off()
        off.symmetric_difference_update({int(data[5:])})
        put("days_off", ",".join(map(str, sorted(off))))
        return edit(*settings_view())
    if data == "aset_hours":
        states[uid] = {"s": "hours"}
        return send(chat, "Введите часы работы в формате <code>10:00-19:00</code>")
    if data == "aset_slot":
        states[uid] = {"s": "slot"}
        return send(chat, "Введите длительность записи в минутах (15–480), например 90")
    if data == "abroad":
        states[uid] = {"s": "broadcast"}
        return send(chat, "Введите текст рассылки всем, кто когда-либо записывался (отмена: /cancel):")
    if data == "aexport":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["ID", "Дата", "Время", "Услуга", "Цена", "Имя", "Телефон", "Username", "Статус", "Создана"])
        for r in db.execute("SELECT * FROM bookings ORDER BY date, time"):
            w.writerow([r["id"], r["date"], r["time"], r["service"] or "", r["price"] or "", r["name"], r["phone"], r["username"] or "",
                        r["status"], r["created_at"]])
        return send_document(chat, "bookings.csv", buf.getvalue().encode("utf-8-sig"))


# ───────────────────────── Запуск ─────────────────────────
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    me = api("getMe")
    if not me:
        raise SystemExit("Не удалось подключиться. Проверьте BOT_TOKEN и интернет.")
    print(f"Бот @{me['username']} запущен. Остановить: Ctrl+C")
    offset = None
    while True:
        updates = api("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
        if updates is None:
            _time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    on_message(u["message"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
            except Exception:
                logging.exception("handler error")


if __name__ == "__main__":
    main()
