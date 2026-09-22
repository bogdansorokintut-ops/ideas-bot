"""Текст сообщения → список Idea. Без LLM, по простым правилам.

Идеи разделяются пустой строкой. Внутри идеи первая строка — название,
строки вида `метка: значение` уходят в поля (см. LABELS), незнакомые метки — в extras,
всё остальное — в описание. Блок, который начинается не с названия, считается
продолжением предыдущей идеи.
"""

import re

from models import Idea

MAX_TITLE = 60
MAX_LABEL_WORDS = 4

LABELS = {
    "описание": "description", "суть": "description", "идея": "description", "концепт": "description",
    "о чём": "description", "о чем": "description",
    "питч": "pitch", "pitch": "pitch",
    "жанр": "genre", "genre": "genre", "теги": "genre",
    "loop": "core_loop", "core loop": "core_loop", "луп": "core_loop",
    "механика": "core_loop", "механика-изюминка": "core_loop", "изюминка": "core_loop", "геймплей": "core_loop",
    "хук": "hook", "hook": "hook", "фишка": "hook",
    "почему это вирально": "hook", "почему вирально": "hook", "вирально": "hook",
    "референсы": "references", "рефы": "references", "refs": "references", "похоже на": "references",
    "развитие": "growth", "дальше": "growth", "потенциал": "growth", "расширение": "growth",
}

FORMAT_HINT = (
    "Первая строка — название, дальше описание. Необязательные поля:\n"
    "жанр: roguelite, management\n"
    "хук: корабль разбивается о скалы в свете маяка\n"
    "loop: ночь — заманиваешь, утро — лутаешь\n"
    "рефы: Dredge, Reigns\n"
    "развитие: мультиплеер, сезоны\n\n"
    "Несколько идей — через пустую строку. Незнакомые метки («Игроков: 4–8») тоже сохранятся."
)


def _split_label(line: str) -> tuple[str, str] | None:
    """«Метка: значение» → (метка, значение), иначе None. Ссылки и длинные «метки» не считаются."""
    key, sep, value = line.partition(":")
    key, value = key.strip(), value.strip()
    if not sep or not key or not value or value.startswith("//"):
        return None
    if len(key.split()) > MAX_LABEL_WORDS or len(key) > 40:
        return None
    return key, value


def _is_title(line: str) -> bool:
    return len(line) <= MAX_TITLE and _split_label(line) is None


def _absorb(idea: Idea, lines: list[str]) -> None:
    for line in lines:
        parsed = _split_label(line)
        if parsed is None:
            idea.description = f"{idea.description} {line}".strip()
            continue
        key, value = parsed
        name = LABELS.get(" ".join(key.lower().split()))
        if name:
            current = getattr(idea, name)
            setattr(idea, name, f"{current} {value}".strip() if current else value)
        else:
            idea.extras[key] = f"{idea.extras[key]} {value}".strip() if key in idea.extras else value


def _cut_title(line: str) -> str:
    return line[: MAX_TITLE - 1].rsplit(" ", 1)[0] + "…" if len(line) > MAX_TITLE else line


def _new_idea(lines: list[str], author: str, raw: str) -> Idea:
    first = lines[0]
    if _is_title(first):
        idea = Idea(title=first, author=author, raw=raw)
        _absorb(idea, lines[1:])
        return idea
    # Строки с названием нет: блок начинается с самой идеи или с метки («Игроков: 4–8» —
    # например, если название осталось в предыдущем куске разрезанного сообщения).
    # Название режем из первой свободной строки, не из метки; LLM потом подберёт нормальное.
    text_line = next((line for line in lines if _split_label(line) is None), first)
    idea = Idea(title=_cut_title(text_line), author=author, raw=raw, auto_title=True)
    _absorb(idea, lines)
    return idea


def parse_ideas(text: str, author: str) -> list[Idea]:
    ideas: list[Idea] = []
    for chunk in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in chunk.splitlines() if line.strip()]
        if not lines:
            continue
        if ideas and not _is_title(lines[0]):
            _absorb(ideas[-1], lines)
            ideas[-1].raw = f"{ideas[-1].raw}\n\n{chunk.strip()}"
        else:
            ideas.append(_new_idea(lines, author, chunk.strip()))
    return ideas


def parse_idea(text: str, author: str) -> Idea:
    """Первая идея из текста — для простых случаев и тестов."""
    return parse_ideas(text, author)[0]
