"""Понимание свободного текста: что пользователь хочет от бота. Работает только с LLM."""

import re

import llm

SYSTEM = """Ты — ассистент бэклога идей для игр в Telegram. Пользователь пишет свободным текстом.
Определи намерение и ответь одним JSON-объектом с ключами action, title, field, text, intro, reply.

action:
  "create"     — записать новую идею (или несколько). Текст идеи НЕ переписывай и не копируй.
                 В intro положи вступление, которое стоит В НАЧАЛЕ сообщения и не относится к идее
                 («запиши идею:», «вот ещё мысль —»), дословно; если сообщение и есть идея — null.
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


def text_for_create(original: str, intro: str | None) -> str:
    """Текст идеи для парсера: оригинал без вступления. Вступление называет LLM, но режем оригинал сами —
    просить модель переписать идею «дословно» нельзя, на длинных сообщениях она теряет строки."""
    text = original.strip()
    words = (intro or "").split()
    norm_text, norm_intro = " ".join(text.split()).lower(), " ".join(words).lower()
    # Сравниваем без учёта переносов строк и до границы слова («запиши» не должно срезать «запишите»)
    if words and norm_text.startswith(norm_intro) and not norm_text[len(norm_intro) : len(norm_intro) + 1].isalnum():
        parts = text.split(maxsplit=len(words))  # хвост — с исходными переносами строк
        text = parts[len(words)].lstrip(" :—–-\n") if len(parts) > len(words) else ""
    return strip_create_prefix(text) or strip_create_prefix(original)
