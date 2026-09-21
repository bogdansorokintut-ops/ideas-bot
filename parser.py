"""Текст сообщения → Idea. Без LLM, по простым правилам.

Первая строка — название. Остальные строки вида `метка: значение` уходят в поля,
всё прочее — в описание.
"""

from models import Idea

MAX_TITLE = 60

LABELS = {
    "питч": "pitch", "pitch": "pitch",
    "жанр": "genre", "genre": "genre",
    "loop": "core_loop", "core loop": "core_loop", "луп": "core_loop",
    "хук": "hook", "hook": "hook",
    "референсы": "references", "рефы": "references", "refs": "references", "похоже на": "references",
}

FORMAT_HINT = (
    "Первая строка — название, дальше описание. Необязательные поля:\n"
    "жанр: roguelite, management\n"
    "хук: корабль разбивается о скалы в свете маяка\n"
    "loop: ночь — заманиваешь, утро — лутаешь\n"
    "рефы: Dredge, Reigns"
)


def parse_idea(text: str, author: str) -> Idea:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    first, rest = lines[0], lines[1:]

    if len(first) <= MAX_TITLE:
        title = first
        desc_lines = []
    else:
        # Длинная первая строка — это не название, а сама идея
        title = first[: MAX_TITLE - 1].rsplit(" ", 1)[0] + "…"
        desc_lines = [first]

    fields: dict[str, str] = {}
    for line in rest:
        key, sep, value = line.partition(":")
        name = LABELS.get(key.strip().lower())
        if sep and name and value.strip():
            fields[name] = value.strip()
        else:
            desc_lines.append(line)

    return Idea(title=title, description=" ".join(desc_lines), author=author, **fields)
