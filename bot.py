import asyncio, html, json, os, sqlite3, datetime as dt
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton as IB,
                           InlineKeyboardMarkup as IM, KeyboardButton as KB, Message,
                           ReplyKeyboardMarkup)

TZ = ZoneInfo("Europe/Moscow")
ICON = {"L": "🟢", "S": "🔵", "P": "🟠", "M": "🟣"}
KIND = {"L": "Лекция", "S": "Семинар", "P": "Физкультура", "M": "Отработка"}
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WDF = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
      "сентября", "октября", "ноября", "декабря"]
SEM_START = dt.date(2026, 8, 31)
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
LESSONS = json.load(open("schedule.json", encoding="utf-8"))
SUBJECTS = sorted({x["subject"] for x in LESSONS}) + ["Другое"]
esc = html.escape
today = lambda: dt.datetime.now(TZ).date()

# ---------- база ----------
DB = sqlite3.connect("bot.db")
DB.row_factory = sqlite3.Row

def init_db():
    DB.executescript("""
    create table if not exists subs(uid integer primary key);
    create table if not exists settings(uid integer primary key, ev integer default 1,
      pre integer default 1, news integer default 1);
    create table if not exists tasks(id integer primary key autoincrement, scope text, owner integer,
      subject text, text text, due text);
    create table if not exists done(task integer, uid integer, primary key(task, uid));
    create table if not exists makeups(id integer primary key autoincrement, uid integer,
      subject text, date text, time text, note text);
    """)
    try:
        DB.execute("alter table makeups add column scope text default 'me'")
    except sqlite3.OperationalError:
        pass
    DB.execute("insert or ignore into settings(uid) select uid from subs")
    DB.commit()
init_db()

def q(sql, *a):
    cur = DB.execute(sql, a); DB.commit(); return cur.fetchall()

# ---------- оформление расписания ----------
def line(x, compact=False):
    t = x["start"] + ("–" + x["end"] if x["end"] else "")
    kind = x.get("kind") or KIND[x["type"]]
    meta = kind + (f" · {esc(x['room'])}" if x["room"] else "")
    if compact:
        return f"{ICON[x['type']]} <b>{t}</b>  {esc(x['subject'])}\n<i>{meta}</i>"
    return f"{ICON[x['type']]} <b>{t}</b>\n{esc(x['subject'])}\n<i>{meta}</i>"

def day_items(d, uid, groups=True):
    items = [x for x in LESSONS if x["date"] == d.isoformat()]
    for r in q("select * from makeups where date=? and (uid=? or (scope='group' and ?))",
               d.isoformat(), uid, 1 if groups else 0):
        grp = r["scope"] == "group"
        items.append(dict(type="M", start=r["time"], end="", subject=r["subject"],
                          kind="Отработка · общая" if grp else "Отработка", room=r["note"] or ""))
    return sorted(items, key=lambda x: x["start"])

def tag(d):
    delta = (d - today()).days
    return {0: " · сегодня", 1: " · завтра"}.get(delta, "")

def day_text(d, uid):
    ls = day_items(d, uid)
    head = f"📅 <b>{WDF[d.weekday()]}, {d.day} {MN[d.month-1]}</b>{tag(d)}"
    return head + "\n\n" + ("\n\n".join(line(x) for x in ls) if ls else "Пар нет 🎉")

def week_text(mon, uid):
    wn = (mon - SEM_START).days // 7 + 1
    end = mon + dt.timedelta(days=5)
    rng = f"{mon.day}–{end.day} {MN[end.month-1]}" if mon.month == end.month \
        else f"{mon.day} {MN[mon.month-1]} – {end.day} {MN[end.month-1]}"
    parts = []
    for i in range(6):
        d = mon + dt.timedelta(days=i); ls = day_items(d, uid)
        if ls:
            parts.append(f"<b>{WD[d.weekday()]}, {d.day} {MN[d.month-1]}</b>{tag(d)}\n"
                         + "\n".join(line(x, True) for x in ls))
    body = "\n\n".join(parts) or "Пар нет 🎉"
    return f"🗓 <b>Неделя {wn} · {rng}</b>\n\n{body}"

def week_kb(mon):
    p, n = mon - dt.timedelta(days=7), mon + dt.timedelta(days=7)
    return IM(inline_keyboard=[[IB(text="‹ Назад", callback_data=f"w:{p}"),
                                IB(text="Вперёд ›", callback_data=f"w:{n}")]])

# ---------- даты ----------
def parse_date(s):
    for f in ("%d.%m.%Y", "%d.%m"):
        try:
            d = dt.datetime.strptime(s.strip(), f).date()
            return (d.replace(year=2026) if f == "%d.%m" else d).isoformat()
        except ValueError:
            pass

def fmt(iso):
    d = dt.date.fromisoformat(iso); return f"{d.day} {MN[d.month-1]}"

# ---------- задания ----------
def task_rows(uid):
    return q("""select * from tasks where (scope='group' and id not in (select task from done where uid=?))
                or (scope='me' and owner=?) order by coalesce(due,'9999'), id""", uid, uid)

def due_mark(due):
    if not due: return ""
    delta = (dt.date.fromisoformat(due) - today()).days
    if delta < 0: return f" · ⚠️ просрочено ({fmt(due)})"
    if delta <= 1: return f" · 🔥 {'сегодня' if delta == 0 else 'завтра'}"
    return f" · до {fmt(due)}"

def tasks_view(uid):
    rows = task_rows(uid)[:12]
    if not rows:
        return "📝 <b>Задания</b>\n\nВсё сделано 🎉", None
    out = ["📝 <b>Задания</b>"]
    for sc, title in (("group", "👥 Общие"), ("me", "👤 Личные")):
        part = [r for r in rows if r["scope"] == sc]
        if part:
            out.append(f"\n<b>{title}</b>")
            for r in part:
                out.append(f"\n<b>#{r['id']} {esc(r['subject'])}</b>{due_mark(r['due'])}\n{esc(r['text'])}")
    kb = []
    for r in rows:
        b = [IB(text=f"✓ #{r['id']}", callback_data=f"ok:{r['id']}")]
        if r["owner"] == uid or uid in ADMINS:
            b.append(IB(text=f"🗑 #{r['id']}", callback_data=f"rm:{r['id']}"))
        kb.append(b)
    return "\n".join(out), IM(inline_keyboard=kb)

# ---------- отработки ----------
def makeups_view(uid):
    rows = q("select * from makeups where date>=? and (scope='group' or uid=?) order by date, time",
             today().isoformat(), uid)
    if not rows:
        return "🟣 <b>Отработки</b>\n\nПока нет", None
    out, kb = ["🟣 <b>Отработки</b>"], []
    for r in rows:
        who = "👥 общая" if r["scope"] == "group" else "👤 личная"
        out.append(f"\n<b>{fmt(r['date'])}, {r['time']}</b>\n{esc(r['subject'])}\n"
                   f"<i>{who}{' · ' + esc(r['note']) if r['note'] else ''}</i>")
        if r["uid"] == uid or uid in ADMINS:
            kb.append([IB(text=f"🗑 {fmt(r['date'])}, {r['time']}", callback_data=f"mr:{r['id']}")])
    return "\n".join(out), IM(inline_keyboard=kb) if kb else None

# ---------- настройки напоминаний ----------
def get_settings(uid):
    q("insert or ignore into settings(uid) values(?)", uid)
    return q("select * from settings where uid=?", uid)[0]

def settings_view(uid):
    r = get_settings(uid)
    mk = lambda v: "✅" if v else "⬜️"
    txt = ("🔔 <b>Напоминания</b>\n\nВыбери, что присылать. Любой пункт можно выключить, "
           "остальные продолжат работать.")
    kb = IM(inline_keyboard=[
        [IB(text=f"{mk(r['ev'])} Вечером: расписание на завтра", callback_data="st:ev")],
        [IB(text=f"{mk(r['pre'])} За 30 минут до пары", callback_data="st:pre")],
        [IB(text=f"{mk(r['news'])} Новые общие задания и отработки", callback_data="st:news")]])
    return txt, kb

# ---------- меню ----------
B_TODAY, B_TOM, B_WEEK = "📅 Сегодня", "🌅 Завтра", "🗓 Неделя"
B_TASKS, B_ADDT = "📝 Задания", "➕ Задание"
B_MK, B_ADDM = "🟣 Отработки", "➕ Отработка"
B_SET = "🔔 Напоминания"
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KB(text=B_TODAY), KB(text=B_TOM), KB(text=B_WEEK)],
    [KB(text=B_TASKS), KB(text=B_ADDT)],
    [KB(text=B_MK), KB(text=B_ADDM)],
    [KB(text=B_SET)]])

dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Расписание группы 250</b>\n\n🟢 лекция · 🔵 семинар\n🟠 физкультура · 🟣 отработка\n\n"
                   "Напоминания включаются в разделе «🔔 Напоминания».", reply_markup=MENU)

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Отменено", reply_markup=MENU)

@dp.message(F.text == B_TODAY)
async def t_today(m: Message, state: FSMContext):
    await state.clear(); await m.answer(day_text(today(), m.from_user.id))

@dp.message(F.text == B_TOM)
async def t_tom(m: Message, state: FSMContext):
    await state.clear(); await m.answer(day_text(today() + dt.timedelta(days=1), m.from_user.id))

@dp.message(F.text == B_WEEK)
async def t_week(m: Message, state: FSMContext):
    await state.clear()
    mon = today() - dt.timedelta(days=today().weekday())
    await m.answer(week_text(mon, m.from_user.id), reply_markup=week_kb(mon))

@dp.callback_query(F.data.startswith("w:"))
async def cb_week(c: CallbackQuery):
    mon = dt.date.fromisoformat(c.data[2:])
    await c.message.edit_text(week_text(mon, c.from_user.id), reply_markup=week_kb(mon))
    await c.answer()

# ----- напоминания -----
@dp.message(F.text == B_SET)
@dp.message(Command("remind"))
async def t_settings(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = settings_view(m.from_user.id); await m.answer(txt, reply_markup=kb)

@dp.callback_query(F.data.regexp(r"^st:(ev|pre|news)$"))
async def cb_settings(c: CallbackQuery):
    col = c.data[3:]
    get_settings(c.from_user.id)
    q(f"update settings set {col}=1-{col} where uid=?", c.from_user.id)
    txt, kb = settings_view(c.from_user.id)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer("Сохранено")

# ----- задания -----
@dp.message(F.text == B_TASKS)
async def t_tasks(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = tasks_view(m.from_user.id); await m.answer(txt, reply_markup=kb)

@dp.callback_query(F.data.regexp(r"^(ok|rm):\d+$"))
async def cb_task(c: CallbackQuery):
    act, tid = c.data.split(":"); tid = int(tid); uid = c.from_user.id
    r = q("select * from tasks where id=?", tid)
    if r:
        r = r[0]
        if act == "ok":
            if r["scope"] == "me": q("delete from tasks where id=?", tid)
            else: q("insert or ignore into done values(?,?)", tid, uid)
        elif r["owner"] == uid or uid in ADMINS:
            q("delete from tasks where id=?", tid); q("delete from done where task=?", tid)
    txt, kb = tasks_view(uid)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer("Готово")

class T(StatesGroup):
    subject = State(); text = State(); due = State()

def subj_kb(prefix):
    return IM(inline_keyboard=[[IB(text=s, callback_data=f"{prefix}:{i}")] for i, s in enumerate(SUBJECTS)])

def scope_kb(prefix):
    return IM(inline_keyboard=[[IB(text="👥 Общее для группы" if prefix == "ts" else "👥 Общая (видят все)",
                                   callback_data=f"{prefix}:group"),
                                IB(text="👤 Личное" if prefix == "ts" else "👤 Личная",
                                   callback_data=f"{prefix}:me")]])

async def notify_news(bot: Bot, author: int, text: str):
    for r in q("select uid from settings where news=1 and uid!=?", author):
        try: await bot.send_message(r["uid"], text)
        except Exception: pass

@dp.message(F.text == B_ADDT)
async def add_task(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Какое задание?", reply_markup=scope_kb("ts"))

@dp.callback_query(F.data.regexp(r"^ts:(group|me)$"))
async def task_scope(c: CallbackQuery, state: FSMContext):
    await state.update_data(scope=c.data[3:]); await state.set_state(T.subject)
    await c.message.edit_text("Предмет:", reply_markup=subj_kb("tj")); await c.answer()

@dp.callback_query(F.data.startswith("tj:"), StateFilter(T.subject))
async def task_subj(c: CallbackQuery, state: FSMContext):
    await state.update_data(subject=SUBJECTS[int(c.data[3:])]); await state.set_state(T.text)
    await c.message.edit_text("Напиши задание одним сообщением (/cancel — отмена)"); await c.answer()

@dp.message(StateFilter(T.text), F.text)
async def task_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text); await state.set_state(T.due)
    await m.answer("Срок: дата вида 12.11 или 12.11.2026. Если срока нет, отправь «-»")

@dp.message(StateFilter(T.due), F.text)
async def task_due(m: Message, state: FSMContext, bot: Bot):
    due = None
    if m.text.strip() != "-":
        due = parse_date(m.text)
        if not due:
            return await m.answer("Не понял дату. Пример: 12.11. Или «-», если срока нет")
    d = await state.get_data(); await state.clear()
    q("insert into tasks(scope,owner,subject,text,due) values(?,?,?,?,?)",
      d["scope"], m.from_user.id, d["subject"], d["text"], due)
    await m.answer("Задание добавлено ✓", reply_markup=MENU)
    if d["scope"] == "group":
        await notify_news(bot, m.from_user.id,
                          f"📝 <b>Новое общее задание</b>\n\n<b>{esc(d['subject'])}</b>{due_mark(due)}\n{esc(d['text'])}")

# ----- отработки -----
class M(StatesGroup):
    subject = State(); date = State(); time = State(); note = State()

@dp.message(F.text == B_MK)
async def t_makeups(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = makeups_view(m.from_user.id); await m.answer(txt, reply_markup=kb)

@dp.callback_query(F.data.regexp(r"^mr:\d+$"))
async def cb_mr(c: CallbackQuery):
    q("delete from makeups where id=? and (uid=? or ?)", int(c.data[3:]), c.from_user.id,
      1 if c.from_user.id in ADMINS else 0)
    txt, kb = makeups_view(c.from_user.id)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer("Удалено")

@dp.message(F.text == B_ADDM)
async def add_mk(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Какая отработка?", reply_markup=scope_kb("ms"))

@dp.callback_query(F.data.regexp(r"^ms:(group|me)$"))
async def mk_scope(c: CallbackQuery, state: FSMContext):
    await state.update_data(scope=c.data[3:]); await state.set_state(M.subject)
    await c.message.edit_text("Предмет отработки:", reply_markup=subj_kb("mj")); await c.answer()

@dp.callback_query(F.data.startswith("mj:"), StateFilter(M.subject))
async def mk_subj(c: CallbackQuery, state: FSMContext):
    await state.update_data(subject=SUBJECTS[int(c.data[3:])]); await state.set_state(M.date)
    await c.message.edit_text("Дата, например 12.11 (/cancel — отмена)"); await c.answer()

@dp.message(StateFilter(M.date), F.text)
async def mk_date(m: Message, state: FSMContext):
    d = parse_date(m.text)
    if not d: return await m.answer("Не понял дату. Пример: 12.11")
    await state.update_data(date=d); await state.set_state(M.time)
    await m.answer("Время начала, например 15:00")

@dp.message(StateFilter(M.time), F.text)
async def mk_time(m: Message, state: FSMContext):
    try: t = dt.datetime.strptime(m.text.strip().replace(".", ":"), "%H:%M").strftime("%H:%M")
    except ValueError: return await m.answer("Не понял время. Пример: 15:00")
    await state.update_data(time=t); await state.set_state(M.note)
    await m.answer("Кабинет, преподаватель или заметка. Если не нужно, отправь «-»")

@dp.message(StateFilter(M.note), F.text)
async def mk_note(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data(); await state.clear()
    note = "" if m.text.strip() == "-" else m.text
    q("insert into makeups(uid,subject,date,time,note,scope) values(?,?,?,?,?,?)",
      m.from_user.id, d["subject"], d["date"], d["time"], note, d["scope"])
    await m.answer(f"Отработка добавлена: {fmt(d['date'])}, {d['time']} ✓", reply_markup=MENU)
    if d["scope"] == "group":
        await notify_news(bot, m.from_user.id,
                          f"🟣 <b>Новая общая отработка</b>\n\n<b>{fmt(d['date'])}, {d['time']}</b>\n"
                          f"{esc(d['subject'])}" + (f"\n<i>{esc(note)}</i>" if note else ""))

# ----- резервная копия -----
@dp.message(Command("backup"))
async def backup(m: Message):
    if m.from_user.id in ADMINS:
        await m.answer_document(FSInputFile("bot.db"))

@dp.message(F.document)
async def restore(m: Message, bot: Bot):
    if m.from_user.id in ADMINS and m.document.file_name == "bot.db":
        await bot.download(m.document, destination="restore.db")
        src = sqlite3.connect("restore.db"); src.backup(DB); src.close()
        init_db(); await m.answer("База восстановлена ✓")

# ----- рассылки -----
async def reminders(bot: Bot):
    sent = set()
    while True:
        now = dt.datetime.now(TZ).replace(second=0, microsecond=0)
        if now.hour == 20 and now.minute == 0 and ("eve", now.date()) not in sent:
            sent.add(("eve", now.date())); tom = now.date() + dt.timedelta(days=1)
            for r in q("select uid from settings where ev=1"):
                uid, msg = r["uid"], ""
                if day_items(tom, uid): msg = day_text(tom, uid)
                due = [t for t in task_rows(uid) if t["due"] == tom.isoformat()]
                if due:
                    msg += ("\n\n" if msg else "") + "🔥 <b>Срок завтра</b>\n" + "\n".join(
                        f"• {esc(t['subject'])}: {esc(t['text'])}" for t in due)
                if msg:
                    try: await bot.send_message(uid, msg)
                    except Exception: pass
        if now.hour == 20 and now.minute == 5 and ("bk", now.date()) not in sent:
            sent.add(("bk", now.date()))
            for aid in ADMINS:
                try: await bot.send_document(aid, FSInputFile("bot.db"), caption="Копия базы")
                except Exception: pass
        for r in q("select uid from settings where pre=1"):
            uid = r["uid"]
            for x in day_items(now.date(), uid, groups=False):
                start = dt.datetime.fromisoformat(f"{now.date()}T{x['start']}").replace(tzinfo=TZ)
                key = (uid, now.date(), x["start"], x["subject"])
                if start - now == dt.timedelta(minutes=30) and key not in sent:
                    sent.add(key)
                    try: await bot.send_message(uid, "⏰ <b>Через 30 минут</b>\n\n" + line(x))
                    except Exception: pass
        await asyncio.sleep(20)

async def main():
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(reminders(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
