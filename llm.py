"""Дополнение карточки через любой OpenAI-совместимый LLM (Groq, Gemini, Ollama, OpenRouter, ...).

Включается, если в .env заданы LLM_BASE_URL и LLM_MODEL. Заполняет только пустые поля:
то, что пользователь указал явно через метки, не трогается. Если LLM недоступен — карточка
уходит как есть, бот не падает.
"""

import json
import logging
import os
import re

from models import Idea

SYSTEM = """Ты помогаешь двум инди-разработчикам вести бэклог идей игр для Steam (делают на Unity).
Из текста идеи заполни JSON с ключами:
  "title"      - короткое рабочее название, 1-4 слова
  "pitch"      - питч в одну строку: «X meets Y» или «ты - Z, который делает W»
  "genre"      - жанр / Steam-теги через запятую
  "core_loop"  - что игрок делает раз за разом, одна-две фразы
  "hook"       - главная фишка, что покажешь в 10-секундном гифе
  "references" - похожие реальные игры через запятую, до 3 штук
  "growth"     - куда идею можно развить (режимы, механики, контент), одна-две фразы
Пиши по-русски, коротко. Не выдумывай того, чего нет в тексте: если непонятно - дай самое
очевидное предположение и добавь в конце слово (предположение). Если совсем нечего сказать - пустая строка.
Ответь только JSON-объектом, без пояснений и без markdown."""

FIELDS = ("pitch", "genre", "core_loop", "hook", "references", "growth")

_client = None


def enabled() -> bool:
    return bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL"))


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.getenv("LLM_API_KEY") or "none",  # Ollama ключ не нужен, но SDK требует строку
            timeout=60,
        )
    return _client


def _extract_json(text: str) -> dict:
    # Мелкие модели любят оборачивать в ```json``` или дописывать текст — берём первый {...}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def ask_json(system: str, text: str) -> dict:
    """Один запрос к LLM, ответ — JSON-объект (пустой dict, если модель вернула мусор)."""
    from openai import BadRequestError

    client = _get_client()
    kwargs = dict(
        model=os.environ["LLM_MODEL"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        temperature=0.2,
    )
    try:
        resp = await client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
    except BadRequestError:
        # Не все провайдеры умеют json_object — пробуем без него
        resp = await client.chat.completions.create(**kwargs)
    return _extract_json(resp.choices[0].message.content or "")


def _as_str(value) -> str:
    if isinstance(value, list):
        return ", ".join(" ".join(str(v).split()) for v in value if str(v).strip())
    return " ".join(str(value).split()) if value is not None else ""


async def enrich(idea: Idea, text: str) -> Idea:
    try:
        data = await ask_json(SYSTEM, text)
    except Exception as exc:
        logging.warning("LLM недоступен, карточка без дополнения: %s", exc)
        return idea

    for name in FIELDS:
        if not getattr(idea, name) and data.get(name):
            setattr(idea, name, _as_str(data[name]))

    # Название берём у LLM, только если парсер его обрезал из длинной строки
    if idea.title.endswith("…") and data.get("title"):
        idea.title = _as_str(data["title"])

    return idea
