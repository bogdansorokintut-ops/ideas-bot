import asyncio
import html
import logging
import os
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from dotenv import load_dotenv

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
    "Кидай идею игры текстом — запишу строкой в таблицу.\n\n"
    f"{FORMAT_HINT}\n\n"
    "В личке — просто пиши. В группе — /idea <текст>.\n"
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
    await handle_idea(msg, command.args)


@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def private_text(msg: Message):
    await handle_idea(msg, msg.text)


async def handle_idea(msg: Message, text: str):
    if not _allowed(msg.from_user):
        await msg.answer("Этот бот приватный.")
        return

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
