"""Понимание свободного текста: что пользователь хочет от бота. Работает только с LLM."""

import re

import llm

SYSTEM = """Ты — ассистент бэклога идей для игр в Telegram. Пользователь пишет свободным текстом.
Определи намерение и ответь одним JSON-объектом с ключами action, title, field, text, reply.

action:
  "create"     — записать новую идею (или несколько). В text скопируй текст идеи ДОСЛОВНО,
                 с переносами строк, убрав только вступление вроде «запиши идею».
  "append"     — дополнить существующую идею. title — точное название из списка ниже.
                 field — куда: description, pitch, genre, core_loop, hook, references, growth
                 или notes (если не сказано явно — notes). text — что добавить, дословно.
  "set_status" — сменить статус идеи. title из списка, text — новый статус (например «прототип», «в работе», «убито»).
  "list"       — показать список идей.
  "chat"       — всё остальное: вопрос, приветствие, непонятно. reply — короткий ответ по-русски.

Если пользователь ссылается на идею, которой нет в списке — action "chat", в reply скажи, что не нашёл,
и предложи похожие названия из списка. Незаполненные ключи — null. Ответь только JSON.

Существующие идеи:
{titles}"""

FIELD_LABELS = {
    "description": "Описание",
    "pitch": "Питч",
    "genre": "Жанр",
    "core_loop": "Core loop",
    "hook": "Хук",
    "references": "Референсы",
    "growth": "Развитие",
}

_CREATE_PREFIX = re.compile(
    r"^\s*(запиши|запиши-ка|добавь|сохрани|занеси|создай)\s+(новую\s+)?(идею|идея)\s*[:\-—]?\s*", re.I
)


def strip_create_prefix(text: str) -> str:
    return _CREATE_PREFIX.sub("", text, count=1).strip()


async def intent(text: str, titles: list[str]) -> dict:
    """{'action': ..., 'title': ..., 'field': ..., 'text': ..., 'reply': ...}; {} если LLM не ответил."""
    listing = "\n".join(f"- {t}" for t in titles) or "(пока ни одной)"
    try:
        data = await llm.ask_json(SYSTEM.replace("{titles}", listing), text)
    except Exception:
        return {}
    return data if isinstance(data, dict) and data.get("action") else {}


def text_for_create(original: str, suggested: str | None) -> str:
    """Текст идеи для парсера: версия от LLM, если она не потеряла содержимое, иначе оригинал без вступления."""
    fallback = strip_create_prefix(original)
    if suggested and len(suggested) >= 0.6 * len(fallback):
        return suggested
    return fallback
