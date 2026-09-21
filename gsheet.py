"""Google Таблица: лист «сводка» + отдельный лист на каждую идею.

Сводка — первый лист (по sheetId, так что переименование/перестановка не ломает).
Название в сводке — ссылка на лист идеи; питч, жанр и статус — формулы на ячейки этого листа,
поэтому правки на листе идеи видны в сводке сразу.
"""

import base64
import difflib
import json
import os
import re
from datetime import date

from google.oauth2 import service_account
from googleapiclient.discovery import build

from models import Idea

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

INDEX_HEADERS = ["Дата", "Автор", "Название", "Питч", "Жанр", "Статус"]
DEFAULT_STATUS = "идея"

# Карточка на листе идеи: A1 — название, дальше с 3-й строки пары «метка | значение».
CARD_FIRST_ROW = 3
CARD_FIELDS = ["Описание", "Питч", "Жанр", "Core loop", "Хук", "Референсы", "Развитие", "Автор", "Дата", "Статус"]
NOTES_LABEL = "Заметки"

MAX_TAB_TITLE = 90  # лимит Sheets — 100, оставляем место под « (2)»


def _q(tab: str) -> str:
    """Имя листа для диапазона: 'Злой маяк'!A1."""
    return "'" + tab.replace("'", "''") + "'"


def _card_cell(tab: str, label: str) -> str:
    return f"{_q(tab)}!B{CARD_FIRST_ROW + CARD_FIELDS.index(label)}"


def sanitize_tab_title(title: str) -> str:
    cleaned = re.sub(r"[\[\]*?/\\:]", " ", title).strip()
    cleaned = " ".join(cleaned.split())
    return (cleaned or "Идея")[:MAX_TAB_TITLE]


def unique_tab_title(title: str, existing: set[str]) -> str:
    base = sanitize_tab_title(title)
    candidate, n = base, 2
    while candidate in existing:
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def card_rows(idea: Idea) -> list[list[str]]:
    values = {
        "Описание": idea.description,
        "Питч": idea.pitch,
        "Жанр": idea.genre,
        "Core loop": idea.core_loop,
        "Хук": idea.hook,
        "Референсы": idea.references,
        "Развитие": idea.growth,
        "Автор": idea.author,
        "Дата": idea.created.isoformat(),
        "Статус": DEFAULT_STATUS,
    }
    rows = [[idea.title], []]
    rows += [[label, values[label]] for label in CARD_FIELDS]
    rows += [[label, value] for label, value in idea.extras.items()]
    rows += [[], [NOTES_LABEL, ""]]
    return rows


def index_row(idea: Idea, tab: str, tab_id: int) -> list[str]:
    title = idea.title.replace('"', '""')

    def ref(label: str) -> str:
        cell = _card_cell(tab, label)
        return f'=IF({cell}="";"";{cell})'

    return [
        idea.created.isoformat(),
        idea.author,
        f'=HYPERLINK("#gid={tab_id}";"{title}")',
        ref("Питч"),
        ref("Жанр"),
        ref("Статус"),
    ]


def index_format_requests(sheet_id: int) -> list[dict]:
    """Сводка: жирная закреплённая шапка, перенос текста."""
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
    ]


def card_format_requests(sheet_id: int, n_rows: int) -> list[dict]:
    """Лист идеи: крупное название, жирные метки (до строки «Заметки» включительно), широкая колонка значений."""
    last_label_row = n_rows
    return [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                "fields": "userEnteredFormat.textFormat(bold,fontSize)",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": CARD_FIRST_ROW - 1,
                    "endRowIndex": last_label_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": 1,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 120},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "properties": {"pixelSize": 640},
                "fields": "pixelSize",
            }
        },
    ]


class IdeasSheet:
    def __init__(self, spreadsheet_id: str, creds):
        self.id = spreadsheet_id
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.index_id: int | None = None

    @classmethod
    def from_env(cls) -> "IdeasSheet":
        """GOOGLE_CREDENTIALS_JSON (содержимое ключа — JSON или base64, для хостинга)
        или GOOGLE_CREDENTIALS (путь к файлу)."""
        raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
        if raw:
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode()
            creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        else:
            path = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
            creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
        return cls(os.environ["GOOGLE_SHEET_ID"], creds)

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.id}/edit"

    def tab_url(self, sheet_id: int) -> str:
        return f"{self.url}#gid={sheet_id}"

    # --- низкоуровневое ---

    def _sheets(self) -> list[dict]:
        resp = self.svc.spreadsheets().get(
            spreadsheetId=self.id, fields="sheets.properties(sheetId,title)"
        ).execute()
        return [s["properties"] for s in resp["sheets"]]

    def _values(self, rng: str) -> list[list[str]]:
        resp = self.svc.spreadsheets().values().get(spreadsheetId=self.id, range=rng).execute()
        return resp.get("values", [])

    def _write(self, rng: str, rows: list[list[str]], mode: str) -> None:
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.id, range=rng, valueInputOption=mode, body={"values": rows}
        ).execute()

    def _batch(self, requests: list[dict]) -> dict:
        return self.svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": requests}).execute()

    def _index(self, sheets: list[dict]) -> dict:
        return next((s for s in sheets if s["sheetId"] == self.index_id), sheets[0])

    # --- публичное ---

    def ensure_headers(self) -> None:
        sheets = self._sheets()
        index = sheets[0]
        self.index_id = index["sheetId"]
        if self._values(f"{_q(index['title'])}!1:1"):
            return
        self._write(f"{_q(index['title'])}!A1", [INDEX_HEADERS], "RAW")
        self._batch(index_format_requests(index["sheetId"]))

    def append_idea(self, idea: Idea) -> str:
        """Создаёт лист идеи, пишет карточку, добавляет строку в сводку. Возвращает URL листа."""
        sheets = self._sheets()
        index = self._index(sheets)
        tab = unique_tab_title(idea.title, {s["title"] for s in sheets})

        resp = self._batch([{"addSheet": {"properties": {"title": tab}}}])
        tab_id = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

        rows = card_rows(idea)
        self._write(f"{_q(tab)}!A1", rows, "RAW")
        self._batch(card_format_requests(tab_id, len(rows)))

        self.svc.spreadsheets().values().append(
            spreadsheetId=self.id,
            range=f"{_q(index['title'])}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [index_row(idea, tab, tab_id)]},
        ).execute()
        return self.tab_url(tab_id)

    def list_ideas(self) -> list[tuple[str, str]]:
        """(название, url) для каждого листа идеи, в порядке создания."""
        sheets = self._sheets()
        index_id = self._index(sheets)["sheetId"]
        return [(s["title"], self.tab_url(s["sheetId"])) for s in sheets if s["sheetId"] != index_id]

    def find_idea(self, title: str) -> tuple[str, str] | None:
        """(точное название листа, url) по названию — без учёта регистра, с допуском на опечатки."""
        ideas = self.list_ideas()
        by_lower = {t.lower(): (t, u) for t, u in ideas}
        if title.lower() in by_lower:
            return by_lower[title.lower()]
        close = difflib.get_close_matches(title.lower(), list(by_lower), n=1, cutoff=0.6)
        return by_lower[close[0]] if close else None

    def read_field(self, tab: str, label: str) -> str:
        rows = self._values(_card_cell(tab, label))
        return rows[0][0] if rows and rows[0] else ""

    def write_field(self, tab: str, label: str, value: str) -> None:
        self._write(_card_cell(tab, label), [[value]], "RAW")

    def append_field(self, tab: str, label: str, text: str) -> str:
        """Дописывает текст к полю карточки, возвращает новое значение."""
        current = self.read_field(tab, label)
        value = f"{current} {text}".strip() if current else text
        self.write_field(tab, label, value)
        return value

    def add_note(self, tab: str, text: str) -> None:
        """Новая строка в конец листа идеи (после «Заметки» и предыдущих заметок)."""
        used = len(self._values(f"{_q(tab)}!A:B"))
        self._write(f"{_q(tab)}!A{used + 1}", [[date.today().isoformat(), text]], "RAW")
