"""Focusity PDF exam-schedule parser.

Rule-based (no LLM) FastAPI microservice: extracts exam-schedule tables from
a university PDF using pdfplumber, matches Hebrew header keywords to find
which column is which regardless of the exporting institution's exact
wording/column order, and returns clean JSON rows.
"""
import io
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber

app = FastAPI(title="Focusity PDF Schedule Parser")

# Wide open by default (no cookies/credentials involved, request carries no
# auth) so any Focusity deployment (prod, preview, local dev) can call this
# without keeping an allowlist in sync. Narrow via CORS_ALLOW_ORIGINS if the
# service is ever reused outside Focusity.
_origins = os.environ.get("CORS_ALLOW_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",")] if _origins != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hebrew header keyword -> canonical field. Within a field, more specific
# phrases are listed before generic ones: header cells are matched in
# keyword-list order, so "עד שעה" must be tried as an end_time candidate
# before the bare "שעה" keyword (which also appears inside it) can claim it
# for start_time instead.
FIELD_KEYWORDS = {
    # "שם שיעור" (SCE college's own header wording, confirmed against a real
    # exam-schedule PDF) must stay a full two-word phrase, not a bare
    # "שיעור" -- that would also match "קוד שיעור" (the course-code column)
    # since match_field does substring matching per header cell.
    "course_name": ["שם הקורס", "שם קורס", "שם המקצוע", "שם שיעור", "שם השיעור", "מקצוע", "קורס"],
    "date": ["תאריך הבחינה", "תאריך מבחן", "יום ותאריך", "תאריך"],
    "end_time": ["עד שעה", "שעת סיום", "שעה עד", "סיום"],
    "start_time": ["משעה", "שעת התחלה", "שעה מ", "שעה"],
    "room": ["חדר", "אולם", "מיקום", "בניין"],
    "moed": ["מועד"],
    "course_code": ["קוד קורס", "מספר קורס", "מס' קורס", "קוד"],
}

DATE_RE = re.compile(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")
HEBREW_LETTER_RE = re.compile("[֐-׿]")
RUN_RE = re.compile(r"\d+|\D+")

MIN_HEADER_KEYWORD_HITS = 2


def fix_bidi_line(line: str) -> str:
    """Corrects a real Hebrew-PDF quirk (confirmed against an actual SCE
    college export): some PDF generators draw Hebrew glyphs in an order
    that pdfminer's own RTL heuristic doesn't undo correctly, while
    embedded digit runs (dates/times/course/room codes) come through
    correctly either way -- e.g. a room "ספרא100" extracts as "100ארפס"
    (Hebrew letters flipped, but the "100" digit run itself untouched and
    just relocated). Skip lines with no Hebrew letters at all (pure
    dates/times/codes) entirely. For lines that do have Hebrew, split into
    alternating digit/non-digit runs, reverse the run *order*, and reverse
    characters only within non-digit runs -- proper bidi-style reordering
    that puts a mixed Hebrew+digit cell like a room number back together
    correctly instead of just skipping it."""
    if not HEBREW_LETTER_RE.search(line):
        return line
    runs = RUN_RE.findall(line)
    runs.reverse()
    return "".join(run if run[0].isdigit() else run[::-1] for run in runs)


def normalize_cell(cell, reverse: bool = False) -> str:
    """`reverse` is decided once per table by `detect_header_row` -- see
    its docstring -- and applied per-line via `fix_bidi_line` (a multi-line
    cell can mix a Hebrew line with a digit-only line, e.g. a wrapped
    course-group code, which must not be touched)."""
    if cell is None:
        return ""
    text = str(cell)
    if reverse:
        text = "\n".join(fix_bidi_line(line) for line in text.split("\n"))
    return re.sub(r"\s+", " ", text).strip()


def normalize_date(raw: str) -> Optional[str]:
    """DD/MM/YYYY (or . / - separated, 2- or 4-digit year) -> ISO yyyy-mm-dd."""
    match = DATE_RE.search(raw)
    if not match:
        return None
    day, month, year = match.groups()
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    try:
        return datetime(year_num, int(month), int(day)).date().isoformat()
    except ValueError:
        return None


def normalize_time(raw: str) -> Optional[str]:
    match = TIME_RE.search(raw)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def extract_time_range(raw: str):
    match = TIME_RANGE_RE.search(raw)
    if not match:
        return None, None
    h1, m1, h2, m2 = match.groups()
    return f"{int(h1):02d}:{int(m1):02d}", f"{int(h2):02d}:{int(m2):02d}"


def match_field(header_cell: str) -> Optional[str]:
    if not header_cell:
        return None
    for field, keywords in FIELD_KEYWORDS.items():
        for keyword in keywords:
            if keyword in header_cell:
                return field
    return None


def detect_header_row(table):
    """Pick the (row, needs_reverse) pair -- within the first few rows,
    trying both as-extracted and reversed -- with the most keyword hits,
    so a title row above the real header doesn't get mistaken for it and
    so the reversal quirk (see normalize_cell) is detected per-table
    instead of assumed. Returns (-1, False) if nothing scores high enough."""
    best_idx, best_score, best_reverse = -1, 0, False
    for idx, row in enumerate(table[:5]):
        for reverse in (False, True):
            score = sum(1 for cell in row if match_field(normalize_cell(cell, reverse)))
            if score > best_score:
                best_idx, best_score, best_reverse = idx, score, reverse
    if best_score < MIN_HEADER_KEYWORD_HITS:
        return -1, False
    return best_idx, best_reverse


def map_columns(header_row, reverse: bool) -> dict:
    """field -> column index. First cell to match a field wins, so a
    generic keyword ("שעה") can't steal a column already claimed by a
    more specific one scanned earlier in the same row."""
    mapping = {}
    for idx, raw_cell in enumerate(header_row):
        field = match_field(normalize_cell(raw_cell, reverse))
        if field and field not in mapping:
            mapping[field] = idx
    return mapping


def parse_table(table) -> list:
    if not table:
        return []
    header_idx, reverse = detect_header_row(table)
    if header_idx == -1:
        return []
    mapping = map_columns(table[header_idx], reverse)
    if "course_name" not in mapping or "date" not in mapping:
        return []

    def cell(row, field):
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return normalize_cell(row[idx], reverse)

    records = []
    for row in table[header_idx + 1:]:
        if row is None or all(not normalize_cell(c) for c in row):
            continue

        course_name = cell(row, "course_name")
        if not course_name:
            continue

        date_iso = normalize_date(cell(row, "date"))
        if not date_iso:
            continue

        start_raw = cell(row, "start_time")
        end_raw = cell(row, "end_time")
        start_time = normalize_time(start_raw) if start_raw else None
        end_time = normalize_time(end_raw) if end_raw else None

        # A single time column sometimes holds a full "HH:MM-HH:MM" range
        # (common when the source PDF only exports one time column).
        if start_time and not end_time:
            range_start, range_end = extract_time_range(start_raw)
            if range_start:
                start_time, end_time = range_start, range_end

        record = {
            "course_name": course_name,
            "date": date_iso,
            "start_time": start_time,
            "end_time": end_time,
        }
        room = cell(row, "room")
        if room:
            record["room"] = room
        moed = cell(row, "moed")
        if moed:
            record["moed"] = moed
        course_code = cell(row, "course_code")
        if course_code:
            record["course_code"] = course_code

        records.append(record)

    return records


@app.get("/health")
def health():
    return {"status": "ok"}


# Temporary diagnostic endpoint for designing the weekly-grid parser -- returns
# words with positions (bidi-fixed) plus table/line counts so the real grid
# geometry can be inspected without a local Python interpreter.
# TODO: remove once the class-schedule parser is validated.
@app.post("/debug-class-schedule")
async def debug_class_schedule(file: UploadFile = File(...)):
    contents = await file.read()
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        out_words = [
            {
                "t": fix_bidi_line(w["text"]),
                "raw": w["text"],
                "x0": round(w["x0"], 1),
                "x1": round(w["x1"], 1),
                "top": round(w["top"], 1),
                "bottom": round(w["bottom"], 1),
            }
            for w in words
        ]
        tables = page.extract_tables()
        return {
            "page_width": round(page.width, 1),
            "page_height": round(page.height, 1),
            "num_words": len(out_words),
            "num_lines": len(page.lines),
            "num_rects": len(page.rects),
            "num_tables": len(tables),
            "table0_dims": [len(tables[0]), len(tables[0][0])] if tables and tables[0] else None,
            "words": out_words,
        }


@app.post("/parse-pdf")
async def parse_pdf(file: UploadFile = File(...)):
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="הקובץ שהועלה אינו PDF.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="הקובץ ריק.")

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            records = []
            for page in pdf.pages:
                for table in page.extract_tables():
                    records.extend(parse_table(table))
    except HTTPException:
        raise
    except Exception as exc:  # pdfplumber/pdfminer can raise many exception types on malformed PDFs
        raise HTTPException(status_code=422, detail=f"קריאת ה-PDF נכשלה: {exc}")

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהתה טבלת בחינות בקובץ. ודא שהקובץ מכיל טבלה עם כותרות בעברית (למשל: קורס, תאריך, שעה).",
        )

    return records
