import asyncio
import html
import logging
import os
import re
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from dotenv import load_dotenv

import assistant
import llm
from gsheet import IdeasSheet
from models import Idea
from parser import FORMAT_HINT, parse_ideas

load_dotenv()
logging.basicConfig(level=logging.INFO)

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


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(HELP)


@dp.message(Command("sheet"))
async def cmd_sheet(msg: Message):
    await msg.answer(sheet.url)


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    ideas = await asyncio.to_thread(sheet.list_ideas)
    if not ideas:
        await msg.answer("Пока пусто.")
        return
    lines = [f'{i}. <a href="{url}">{html.escape(title)}</a>' for i, (title, url) in enumerate(ideas, 1)]
    await msg.answer("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.message(Command("idea"))
async def cmd_idea(msg: Message, command: CommandObject):
    if not command.args:
        await msg.answer("Напиши идею после команды: /idea <текст>")
        return
    if not _allowed(msg.from_user):
        await msg.answer("Этот бот приватный.")
        return
    await create_ideas(msg, command.args)


@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def private_text(msg: Message):
    await handle_text(msg, msg.text)


@dp.message(F.text, F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_text(msg: Message):
    """В группе реагируем только на упоминание бота или ответ на его сообщение."""
    me = await msg.bot.me()
    mention = re.compile(rf"@{re.escape(me.username)}\b", re.I)
    replied_to_me = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == me.id)
    if not (mention.search(msg.text) or replied_to_me):
        return
    await handle_text(msg, mention.sub("", msg.text).strip())


async def handle_text(msg: Message, text: str):
    if not _allowed(msg.from_user):
        await msg.answer("Этот бот приватный.")
        return
    if not text:
        await msg.answer(HELP)
        return
    if not llm.enabled():
        await create_ideas(msg, assistant.strip_create_prefix(text))
        return

    await msg.bot.send_chat_action(msg.chat.id, "typing")
    titles = [t for t, _ in await asyncio.to_thread(sheet.list_ideas)]
    intent = await assistant.intent(text, titles)
    action = intent.get("action")

    if action == "create" or not action:
        await create_ideas(msg, assistant.text_for_create(text, intent.get("text")))
    elif action == "append":
        await append_to_idea(msg, intent)
    elif action == "set_status":
        await set_status(msg, intent)
    elif action == "list":
        await cmd_list(msg)
    else:
        await msg.answer(intent.get("reply") or "Не понял. Напиши «запиши идею …» или «добавь … в идею …».")


async def _resolve_idea(msg: Message, intent: dict) -> tuple[str, str] | None:
    title = (intent.get("title") or "").strip()
    found = await asyncio.to_thread(sheet.find_idea, title) if title else None
    if found is None:
        await msg.answer(f"Не нашёл идею «{title}». Посмотри /list.")
    return found


async def append_to_idea(msg: Message, intent: dict):
    text = (intent.get("text") or "").strip()
    if not text:
        await msg.answer("Что именно добавить?")
        return
    found = await _resolve_idea(msg, intent)
    if found is None:
        return
    tab, url = found
    label = assistant.FIELD_LABELS.get(intent.get("field") or "")
    if label:
        await asyncio.to_thread(sheet.append_field, tab, label, text)
    else:
        label = "Заметки"
        await asyncio.to_thread(sheet.add_note, tab, text)
    await msg.answer(
        f'✍️ <a href="{url}">{html.escape(tab)}</a> → {label}: {html.escape(text)}',
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def set_status(msg: Message, intent: dict):
    status = (intent.get("text") or "").strip()
    if not status:
        await msg.answer("Какой статус поставить?")
        return
    found = await _resolve_idea(msg, intent)
    if found is None:
        return
    tab, url = found
    await asyncio.to_thread(sheet.write_field, tab, "Статус", status)
    await msg.answer(
        f'🏷 <a href="{url}">{html.escape(tab)}</a> → статус: {html.escape(status)}',
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def create_ideas(msg: Message, text: str):
    ideas = parse_ideas(text, author=_author(msg))
    if len(ideas) > 1:
        await msg.answer(f"Нашёл {len(ideas)} идеи — подтверди каждую отдельно:")

    for idea in ideas:
        if llm.enabled():
            await msg.bot.send_chat_action(msg.chat.id, "typing")
            idea = await llm.enrich(idea, idea.raw)
        pid = uuid.uuid4().hex[:8]
        pending[pid] = idea
        await msg.answer(_fit(idea.preview()), reply_markup=_keyboard(pid))


def _fit(text: str, limit: int = 4000) -> str:
    """Telegram не принимает сообщения длиннее 4096 символов."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dp.callback_query(F.data.startswith("save:"))
async def on_save(cb: CallbackQuery):
    if not _allowed(cb.from_user):
        await cb.answer("Этот бот приватный.", show_alert=True)
        return
    idea = pending.pop(cb.data.split(":", 1)[1], None) or Idea.from_preview(cb.message.text)
    if idea is None:
        await cb.answer("Не смог восстановить карточку — отправь идею заново", show_alert=True)
        return
    try:
        tab_url = await asyncio.to_thread(sheet.append_idea, idea)
    except Exception:
        logging.exception("append failed")
        await cb.answer("Не удалось записать в таблицу", show_alert=True)
        return
    await cb.message.edit_text(_fit(f"{idea.preview()}\n\n✅ Записано → {tab_url}"))
    await cb.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def on_cancel(cb: CallbackQuery):
    pending.pop(cb.data.split(":", 1)[1], None)
    await cb.message.edit_text("❌ Отменено")
    await cb.answer()


async def main():
    await asyncio.to_thread(sheet.ensure_headers)
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook()  # на случай, если раньше стоял webhook
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
