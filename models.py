from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Idea:
    title: str
    description: str = ""
    pitch: str = ""
    genre: str = ""
    core_loop: str = ""
    hook: str = ""
    references: str = ""
    growth: str = ""
    # Метки, которых нет среди полей выше: «Игроков: 4–8», «Платформа: …». Идут отдельными строками на лист.
    extras: dict[str, str] = field(default_factory=dict)
    author: str = ""
    created: date = field(default_factory=date.today)
    # Исходный текст блока — только для LLM, в таблицу не пишется
    raw: str = field(default="", compare=False, repr=False)

    def fields(self) -> list[tuple[str, str]]:
        """Пары (метка, значение) для карточки; пустые поля пропускаются."""
        rows = [
            ("Описание", self.description),
            ("Питч", self.pitch),
            ("Жанр", self.genre),
            ("Core loop", self.core_loop),
            ("Хук", self.hook),
            ("Референсы", self.references),
            ("Развитие", self.growth),
            *self.extras.items(),
            ("Автор", f"{self.author} · {self.created:%d.%m.%Y}"),
        ]
        return [(k, v.strip()) for k, v in rows if v and v.strip()]

    def preview(self) -> str:
        return "\n".join([f"🎮 {self.title}"] + [f"{k}: {v}" for k, v in self.fields()])

    @classmethod
    def from_preview(cls, text: str | None) -> "Idea | None":
        """Обратно из preview() — чтобы кнопка «Записать» работала после перезапуска бота."""
        lines = (text or "").splitlines()
        if not lines or not lines[0].startswith("🎮 "):
            return None
        idea = cls(title=lines[0][2:].strip())
        for line in lines[1:]:
            label, sep, value = line.partition(": ")
            if not sep:
                continue
            if label == "Автор":
                author, _, day = value.partition(" · ")
                idea.author = author.strip()
                try:
                    idea.created = datetime.strptime(day.strip(), "%d.%m.%Y").date()
                except ValueError:
                    pass
            elif label in PREVIEW_LABELS:
                setattr(idea, PREVIEW_LABELS[label], value.strip())
            else:
                idea.extras[label] = value.strip()
        return idea


PREVIEW_LABELS = {
    "Описание": "description",
    "Питч": "pitch",
    "Жанр": "genre",
    "Core loop": "core_loop",
    "Хук": "hook",
    "Референсы": "references",
    "Развитие": "growth",
}
