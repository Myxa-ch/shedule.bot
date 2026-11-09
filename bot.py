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
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
      "сентября", "октября", "ноября", "декабря"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
LESSONS = json.load(open("schedule.json", encoding="utf-8"))
SUBJECTS = sorted({x["subject"] for x in LESSONS}) + ["Другое"]
esc = html.escape

DB = sqlite3.connect("bot.db")
DB.row_factory = sqlite3.Row
DB.executescript("""
create table if not exists subs(uid integer primary key);
create table if not exists tasks(id integer primary key autoincrement, scope text, owner integer,
  subject text, text text, due text);
create table if not exists done(task integer, uid integer, primary key(task, uid));
create table if not exists makeups(id integer primary key autoincrement, uid integer,
  subject text, date text, time text, note text);
""")
sent = set()
today = lambda: dt.datetime.now(TZ).date()

def q(sql, *a):
    cur = DB.execute(sql, a); DB.commit(); return cur.fetchall()

# ---------- расписание ----------
def day_items(d, uid):
    items = [x for x in LESSONS if x["date"] == d.isoformat()]
    for r in q("select * from makeups where uid=? and date=?", uid, d.isoformat()):
        items.append(dict(type="M", start=r["time"], end="", subject="Отработка · " + r["subject"],
                          room=r["note"] or ""))
    return sorted(items, key=lambda x: x["start"])

def line(x):
    t = x["start"] + ("–" + x["end"] if x["end"] else "")
    room = f" · {esc(x['room'])}" if x["room"] else ""
    return f"{ICON[x['type']]} <b>{t}</b>\n{esc(x['subject'])}{room}"

def day_text(d, uid):
    ls = day_items(d, uid)
    head = f"<b>{WD[d.weekday()]}, {d.day} {MN[d.month-1]}</b>"
    return head + "\n\n" + ("\n\n".join(map(line, ls)) if ls else "Пар нет 🎉")

def week_text(mon, uid):
    days = [mon + dt.timedelta(days=i) for i in range(6)]
    parts = [day_text(d, uid) for d in days if day_items(d, uid)]
    return "\n\n———\n\n".join(parts) or "На этой неделе пар нет"

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

def tasks_view(uid):
    rows = task_rows(uid)[:12]
    if not rows:
        return "Заданий нет 🎉", None
    out, kb = [], []
    for sc, title in (("group", "Общие"), ("me", "Личные")):
        part = [r for r in rows if r["scope"] == sc]
        if part:
            out.append(f"<b>{title}</b>")
            for r in part:
                due = f" · до {fmt(r['due'])}" if r["due"] else ""
                out.append(f"#{r['id']} <b>{esc(r['subject'])}</b>{due}\n{esc(r['text'])}")
    for r in rows:
        b = [IB(text=f"✓ #{r['id']}", callback_data=f"ok:{r['id']}")]
        if r["owner"] == uid or uid in ADMINS:
            b.append(IB(text=f"🗑 #{r['id']}", callback_data=f"rm:{r['id']}"))
        kb.append(b)
    return "\n\n".join(out), IM(inline_keyboard=kb)

def makeups_view(uid):
    rows = q("select * from makeups where uid=? and date>=? order by date, time", uid, today().isoformat())
    if not rows:
        return "Отработок нет", None
    txt = "\n\n".join(f"#{r['id']} 🟣 <b>{fmt(r['date'])}, {r['time']}</b>\n{esc(r['subject'])}"
                      + (f" · {esc(r['note'])}" if r["note"] else "") for r in rows)
    kb = IM(inline_keyboard=[[IB(text=f"🗑 #{r['id']}", callback_data=f"mr:{r['id']}")] for r in rows])
    return txt, kb

# ---------- меню ----------
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KB(text="Сегодня"), KB(text="Завтра"), KB(text="Неделя")],
    [KB(text="Задания"), KB(text="➕ Задание")],
    [KB(text="Отработки"), KB(text="➕ Отработка")],
    [KB(text="Напоминания")]])

dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Расписание группы 250.\n🟢 лекция · 🔵 семинар · 🟠 физкультура · 🟣 отработка",
                   reply_markup=MENU)

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Отменено", reply_markup=MENU)

@dp.message(F.text == "Сегодня")
async def t_today(m: Message, state: FSMContext):
    await state.clear(); await m.answer(day_text(today(), m.from_user.id))

@dp.message(F.text == "Завтра")
async def t_tom(m: Message, state: FSMContext):
    await state.clear(); await m.answer(day_text(today() + dt.timedelta(days=1), m.from_user.id))

@dp.message(F.text == "Неделя")
async def t_week(m: Message, state: FSMContext):
    await state.clear()
    mon = today() - dt.timedelta(days=today().weekday())
    await m.answer(week_text(mon, m.from_user.id), reply_markup=week_kb(mon))

@dp.callback_query(F.data.startswith("w:"))
async def cb_week(c: CallbackQuery):
    mon = dt.date.fromisoformat(c.data[2:])
    await c.message.edit_text(week_text(mon, c.from_user.id), reply_markup=week_kb(mon))
    await c.answer()

@dp.message(F.text == "Напоминания")
@dp.message(Command("remind"))
async def remind(m: Message, state: FSMContext):
    await state.clear()
    if q("select 1 from subs where uid=?", m.from_user.id):
        q("delete from subs where uid=?", m.from_user.id); await m.answer("Напоминания выключены")
    else:
        q("insert into subs values(?)", m.from_user.id)
        await m.answer("Напоминания включены: в 20:00 расписание и сроки на завтра, за 30 минут до пары. "
                       "Ещё приходят новые общие задания.")

# ---------- задания: просмотр ----------
@dp.message(F.text == "Задания")
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

# ---------- задания: добавление ----------
class T(StatesGroup):
    subject = State(); text = State(); due = State()

def subj_kb(prefix):
    rows = [[IB(text=s, callback_data=f"{prefix}:{i}")] for i, s in enumerate(SUBJECTS)]
    return IM(inline_keyboard=rows)

@dp.message(F.text == "➕ Задание")
async def add_task(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Какое задание?", reply_markup=IM(inline_keyboard=[[
        IB(text="Общее для группы", callback_data="ts:group"), IB(text="Личное", callback_data="ts:me")]]))

@dp.callback_query(F.data.startswith("ts:"))
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
    await m.answer("Срок: дата в формате 12.11 или 12.11.2026. Если срока нет, отправь «-»")

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
        for (uid,) in [(r["uid"],) for r in q("select uid from subs where uid!=?", m.from_user.id)]:
            try:
                await bot.send_message(uid, f"Новое общее задание\n<b>{esc(d['subject'])}</b>\n{esc(d['text'])}"
                                       + (f"\nДо {fmt(due)}" if due else ""))
            except Exception:
                pass

# ---------- отработки ----------
class M(StatesGroup):
    subject = State(); date = State(); time = State(); note = State()

@dp.message(F.text == "Отработки")
async def t_makeups(m: Message, state: FSMContext):
    await state.clear()
    txt, kb = makeups_view(m.from_user.id); await m.answer(txt, reply_markup=kb)

@dp.callback_query(F.data.regexp(r"^mr:\d+$"))
async def cb_mr(c: CallbackQuery):
    q("delete from makeups where id=? and uid=?", int(c.data[3:]), c.from_user.id)
    txt, kb = makeups_view(c.from_user.id)
    await c.message.edit_text(txt, reply_markup=kb); await c.answer("Удалено")

@dp.message(F.text == "➕ Отработка")
async def add_mk(m: Message, state: FSMContext):
    await state.clear(); await state.set_state(M.subject)
    await m.answer("Предмет отработки:", reply_markup=subj_kb("mj"))

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
    await m.answer("Кабинет или заметка. Если не нужно, отправь «-»")

@dp.message(StateFilter(M.note), F.text)
async def mk_note(m: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    note = "" if m.text.strip() == "-" else m.text
    q("insert into makeups(uid,subject,date,time,note) values(?,?,?,?,?)",
      m.from_user.id, d["subject"], d["date"], d["time"], note)
    await m.answer(f"Отработка добавлена: {fmt(d['date'])}, {d['time']} ✓", reply_markup=MENU)

# ---------- резервная копия ----------
@dp.message(Command("backup"))
async def backup(m: Message):
    if m.from_user.id in ADMINS:
        await m.answer_document(FSInputFile("bot.db"))

# ---------- напоминания ----------
async def reminders(bot: Bot):
    while True:
        now = dt.datetime.now(TZ).replace(second=0, microsecond=0)
        uids = [r["uid"] for r in q("select uid from subs")]
        if now.hour == 20 and now.minute == 0 and ("eve", now.date()) not in sent:
            sent.add(("eve", now.date())); tom = now.date() + dt.timedelta(days=1)
            for uid in uids:
                msg = ""
                if day_items(tom, uid): msg = "Завтра:\n\n" + day_text(tom, uid)
                due = [r for r in task_rows(uid) if r["due"] == tom.isoformat()]
                if due:
                    msg += ("\n\n" if msg else "") + "Срок завтра:\n" + "\n".join(
                        f"• {esc(r['subject'])}: {esc(r['text'])}" for r in due)
                if msg:
                    try: await bot.send_message(uid, msg)
                    except Exception: pass
        if now.hour == 20 and now.minute == 5 and ("bk", now.date()) not in sent:
            sent.add(("bk", now.date()))
            for aid in ADMINS:
                try: await bot.send_document(aid, FSInputFile("bot.db"), caption="Копия базы")
                except Exception: pass
        for uid in uids:
            for x in day_items(now.date(), uid):
                start = dt.datetime.fromisoformat(f"{now.date()}T{x['start']}").replace(tzinfo=TZ)
                key = (uid, now.date(), x["start"], x["subject"])
                if start - now == dt.timedelta(minutes=30) and key not in sent:
                    sent.add(key)
                    try: await bot.send_message(uid, "Через 30 минут\n\n" + line(x))
                    except Exception: pass
        await asyncio.sleep(20)

async def main():
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(reminders(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
