import asyncio
import html
import logging
import os
import re
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from dotenv import load_dotenv

import assistant
import llm
from gsheet import IdeasSheet
from models import Idea
from parser import FORMAT_HINT, parse_ideas

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ideas-bot")


def _check_env() -> None:
    """Понятная ошибка в логах хостинга вместо KeyError где-то в глубине."""
    missing = [k for k in ("BOT_TOKEN", "GOOGLE_SHEET_ID") if not os.getenv(k)]
    if not os.getenv("GOOGLE_CREDENTIALS_JSON") and not os.path.exists(os.getenv("GOOGLE_CREDENTIALS", "credentials.json")):
        missing.append("GOOGLE_CREDENTIALS_JSON (или файл credentials.json)")
    if missing:
        raise SystemExit(f"Не заданы переменные окружения: {', '.join(missing)}")
    log.info(
        "env ok: sheet=%s, allowed=%s, llm=%s",
        os.environ["GOOGLE_SHEET_ID"][:8] + "…",
        os.getenv("ALLOWED_USERS") or "(все)",
        os.getenv("LLM_MODEL") or "выключен",
    )


_check_env()
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Юзернеймы (без @) и/или числовые id, через запятую. Пусто = пускать всех.
ALLOWED_USERS = {x.strip().lstrip("@").lower() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()}

sheet = IdeasSheet.from_env()
dp = Dispatcher()

# Идеи, ждущие подтверждения кнопкой. Если бот перезапустился и память пуста —
# карточка восстанавливается из текста превью (Idea.from_preview).
pending: dict[str, Idea] = {}

HELP = (
    "Кидай идею игры текстом — заведу лист в таблице.\n\n"
    f"{FORMAT_HINT}\n\n"
    "В личке — просто пиши. В группе — упомяни меня или ответь на моё сообщение.\n"
    "Понимаю и обычные фразы: «запиши идею …», «добавь в «Хор» …», «статус Хора — прототип», «покажи идеи».\n\n"
    "/list — список идей со ссылками на их листы\n"
    "/sheet — ссылка на таблицу"
)

ACK = "⏳ Принял, разбираюсь…"

# Telegram режет сообщения длиннее 4096 символов на несколько подряд идущих. Длинный кусок
# придерживаем и ждём продолжение от того же человека, чтобы разобрать всё вместе —
# иначе название идеи остаётся в одном куске, а её текст в другом.
SPLIT_HINT = 2000  # кусок короче этого — точно не порезанное сообщение, не ждём
SPLIT_WAIT = 2.0  # секунд тишины после последнего куска
_parts: dict[tuple[int, int], list[str]] = {}  # (chat, user) → куски, которые ещё копятся


def _allowed(user: User | None) -> bool:
    if not ALLOWED_USERS:
        return True
    if user is None:
        return False
    keys = {str(user.id)}
    if user.username:
        keys.add(user.username.lower())
    return bool(keys & ALLOWED_USERS)


def _author(msg: Message) -> str:
    u = msg.from_user
    if u is None:
        return "?"
    return f"@{u.username}" if u.username else u.full_name


def _keyboard(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Записать", callback_data=f"save:{pid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel:{pid}"),
            ]
        ]
    )


def _fit(text: str, limit: int = 4000) -> str:
    """Telegram не принимает сообщения длиннее 4096 символов."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _link(title: str, url: str) -> str:
    return f'<a href="{url}">{html.escape(title)}</a>'


def _footer(tab_url: str | None = None) -> str:
    """Ссылки в конец ответа: лист идеи (если есть) и вся таблица."""
    parts = ([_link("Открыть лист", tab_url)] if tab_url else []) + [_link("Таблица", sheet.url)]
    return "\n\n🔗 " + " · ".join(parts)


async def _ack(msg: Message) -> Message:
    """Отбивка «принял» реплаем; если исходное сообщение уже нельзя цитировать — обычным сообщением."""
    try:
        return await msg.reply(ACK)
    except TelegramBadRequest:
        return await msg.answer(ACK)


async def _finish(ack: Message, text: str) -> None:
    """Отбивка «принял» превращается в результат."""
    await ack.edit_text(_fit(text), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def _parts_key(msg: Message) -> tuple[int, int]:
    return msg.chat.id, msg.from_user.id if msg.from_user else 0


def _take_continuation(msg: Message, text: str) -> bool:
    """Если от этого человека уже копится длинное сообщение — кусок туда, отдельно не обрабатываем."""
    parts = _parts.get(_parts_key(msg))
    if parts is None:
        return False
    parts.append(text)
    return True


async def _collect_parts(msg: Message, text: str) -> str:
    """Ждёт, пока куски перестанут приходить, и склеивает их."""
    key = _parts_key(msg)
    parts = _parts[key] = [text]
    while True:
        n = len(parts)
        await asyncio.sleep(SPLIT_WAIT)
        if len(parts) == n:
            break
    del _parts[key]
    return _join_parts(parts)


def _join_parts(parts: list[str]) -> str:
    text = parts[0]
    for part in parts[1:]:
        # Кусок с маленькой буквы — резали посреди предложения, склеиваем пробелом.
        # Иначе резали по переносу строки: разделяем как абзацы — блок без названия
        # парсер и так приклеит к предыдущей идее.
        text += (" " if part[:1].islower() else "\n\n") + part
    return text


# ---------- команды ----------


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(HELP)


@dp.message(Command("sheet"))
async def cmd_sheet(msg: Message):
    await msg.answer(sheet.url)


async def _list_text() -> str:
    ideas = await asyncio.to_thread(sheet.list_ideas)
    if not ideas:
        return "Пока пусто."
    return "\n".join(f"{i}. {_link(title, url)}" for i, (title, url) in enumerate(ideas, 1)) + _footer()


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    await msg.answer(await _list_text(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.message(Command("idea"))
async def cmd_idea(msg: Message, command: CommandObject):
    if not command.args:
        await msg.answer("Напиши идею после команды: /idea <текст>")
        return
    if not _allowed(msg.from_user):
        await msg.answer("Этот бот приватный.")
        return
    ack = await _ack(msg)
    await create_ideas(msg, ack, command.args)


# ---------- свободный текст ----------


@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def private_text(msg: Message):
    await handle_text(msg, msg.text)


@dp.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_text(msg: Message):
    """В группе реагируем только на упоминание бота или ответ на его сообщение."""
    me = await msg.bot.me()
    mention = re.compile(rf"@{re.escape(me.username)}\b", re.I)
    replied_to_me = bool(
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == me.id
    )
    if not (mention.search(msg.text) or replied_to_me or _parts_key(msg) in _parts):
        return
    await handle_text(msg, mention.sub("", msg.text).strip())


async def handle_text(msg: Message, text: str):
    if not _allowed(msg.from_user):
        await msg.answer("Этот бот приватный.")
        return
    if _take_continuation(msg, text):
        return
    if not text:
        await msg.answer(HELP)
        return

    ack = await _ack(msg)
    try:
        if len(text) >= SPLIT_HINT:
            text = await _collect_parts(msg, text)
        await _dispatch(msg, ack, text)
    except Exception as exc:
        logging.exception("handle_text failed")
        await _finish(ack, f"❌ Не получилось: {html.escape(str(exc)[:300])}")


async def _dispatch(msg: Message, ack: Message, text: str):
    if not llm.enabled():
        await create_ideas(msg, ack, assistant.strip_create_prefix(text))
        return

    titles = [t for t, _ in await asyncio.to_thread(sheet.list_ideas)]
    intent = await assistant.intent(text, titles)
    action = intent.get("action")

    if action == "create" or not action:
        await create_ideas(msg, ack, assistant.text_for_create(text, intent.get("intro")))
    elif action == "append":
        await append_to_idea(ack, intent)
    elif action == "set_status":
        await set_status(ack, intent)
    elif action == "list":
        await _finish(ack, await _list_text())
    else:
        await _finish(ack, html.escape(intent.get("reply") or "Не понял. Напиши «запиши идею …» или «добавь … в идею …»."))


async def _resolve_idea(ack: Message, intent: dict) -> tuple[str, str] | None:
    title = (intent.get("title") or "").strip()
    found = await asyncio.to_thread(sheet.find_idea, title) if title else None
    if found is None:
        await _finish(ack, f"🤷 Не нашёл идею «{html.escape(title)}». Посмотри /list.")
    return found


async def append_to_idea(ack: Message, intent: dict):
    text = (intent.get("text") or "").strip()
    if not text:
        await _finish(ack, "Что именно добавить?")
        return
    found = await _resolve_idea(ack, intent)
    if found is None:
        return
    tab, url = found
    label = assistant.FIELD_LABELS.get(intent.get("field") or "")
    if label:
        await asyncio.to_thread(sheet.append_field, tab, label, text)
    else:
        label = "Заметки"
        await asyncio.to_thread(sheet.add_note, tab, text)
    await _finish(ack, f"✍️ {_link(tab, url)} → {label}: {html.escape(text)}{_footer(url)}")


async def set_status(ack: Message, intent: dict):
    status = (intent.get("text") or "").strip()
    if not status:
        await _finish(ack, "Какой статус поставить?")
        return
    found = await _resolve_idea(ack, intent)
    if found is None:
        return
    tab, url = found
    await asyncio.to_thread(sheet.write_field, tab, "Статус", status)
    await _finish(ack, f"🏷 {_link(tab, url)} → статус: {html.escape(status)}{_footer(url)}")


async def create_ideas(msg: Message, ack: Message, text: str):
    ideas = parse_ideas(text, author=_author(msg))
    if not ideas:
        await _finish(ack, "Не нашёл текста идеи.")
        return

    not_enriched: set[int] = set()
    if llm.enabled():
        for n, idea in enumerate(ideas):
            if not await llm.enrich(idea, idea.raw):
                not_enriched.add(n)

    await _finish(ack, f"📝 Разобрал {_plural(len(ideas), 'идею', 'идеи', 'идей')} — подтверди:{_footer()}")
    for n, idea in enumerate(ideas):
        pid = uuid.uuid4().hex[:8]
        pending[pid] = idea
        text = idea.preview()
        if n in not_enriched:  # без «: », чтобы Idea.from_preview не принял строку за поле
            text += "\n\n⚠️ LLM не ответил (лимит или сеть) — пустые поля не дополнены"
        await msg.answer(_fit(text), reply_markup=_keyboard(pid))


# ---------- кнопки ----------


@dp.callback_query(F.data.startswith("save:"))
async def on_save(cb: CallbackQuery):
    if not _allowed(cb.from_user):
        await cb.answer("Этот бот приватный.", show_alert=True)
        return
    pid = cb.data.split(":", 1)[1]
    idea = pending.pop(pid, None) or Idea.from_preview(cb.message.text)
    if idea is None:
        await cb.answer("Не смог восстановить карточку — отправь идею заново", show_alert=True)
        return
    await cb.answer("Записываю…")
    try:
        tab_url = await asyncio.to_thread(sheet.append_idea, idea)
    except Exception as exc:
        logging.exception("append failed")
        pending[pid] = idea  # кнопки остаются — можно нажать ещё раз
        await cb.message.edit_text(
            _fit(f"{idea.preview()}\n\n❌ Не удалось записать: {str(exc)[:200]}"), reply_markup=_keyboard(pid)
        )
        return
    await cb.message.edit_text(
        _fit(f"{html.escape(idea.preview())}\n\n✅ Записано{_footer(tab_url)}"),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(cb: CallbackQuery):
    pending.pop(cb.data.split(":", 1)[1], None)
    await cb.message.edit_text("❌ Отменено")
    await cb.answer()


async def main():
    try:
        await asyncio.to_thread(sheet.ensure_headers)
    except Exception as exc:
        raise SystemExit(f"Нет доступа к таблице: {exc}") from exc
    log.info("таблица доступна: %s", sheet.url)
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook()  # на случай, если раньше стоял webhook
    me = await bot.me()
    log.info("запускаю polling как @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
