"""Focusity PDF exam-schedule parser.

Rule-based (no LLM) FastAPI microservice: extracts exam-schedule tables from
a university PDF using pdfplumber, matches Hebrew header keywords to find
which column is which regardless of the exporting institution's exact
wording/column order, and returns clean JSON rows.
"""
import asyncio
import io
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber

# Previously there was no logging at all: a parse failure's real cause (a
# corrupt PDF, an unexpected pdfminer exception, ...) was visible ONLY by
# echoing the raw exception text back to the client -- which also meant an
# internal detail (a Python exception message, occasionally including a
# library-internal path or type name) was exposed in the HTTP response
# instead of staying server-side. Uvicorn's default logging config already
# ships everything through to stdout/stderr, so this needs no extra setup
# to show up wherever the service's logs are viewed (locally or on Render).
logger = logging.getLogger("focusity_pdf_parser")

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

# Header keyword -> canonical field, Hebrew and English. Within a field,
# more specific phrases are listed before generic ones: header cells are
# matched in keyword-list order, so "עד שעה" must be tried as an end_time
# candidate before the bare "שעה" keyword (which also appears inside it) can
# claim it for start_time instead. The same ordering sensitivity applies
# ACROSS fields too, since match_field checks FIELDS in this dict's own
# order and returns on the first substring hit -- see course_code below.
# English keywords are lowercase; match_field lowercases the header cell
# (Hebrew has no case, so this only affects Latin text). Very short English
# words that hide inside longer common headers ("to" in "instructor", "end"
# in "attendance") are deliberately NOT keywords.
FIELD_KEYWORDS = {
    # Must come before course_name: course_name's own generic "קורס"
    # fallback is a substring of "קוד קורס"/"מספר קורס" (and "course" of
    # "course code"), so if course_name were checked first it would claim
    # the course-code column for itself and course_code's specific keywords
    # would never get a chance to match -- confirmed live (course_code was
    # silently never extracted for any real PDF with a "קוד קורס" column)
    # before this field was moved here.
    "course_code": ["קוד קורס", "מספר קורס", "מס' קורס", "קוד", "course code", "course no", "course id", "code"],
    # "שם שיעור" (SCE college's own header wording, confirmed against a real
    # exam-schedule PDF) must stay a full two-word phrase, not a bare
    # "שיעור" -- that would also match "קוד שיעור" (the course-code column)
    # since match_field does substring matching per header cell. "שם שעור"
    # (no yud) is a second, distinct SCE spelling confirmed against a real
    # grade-sheet PDF from the same college -- both variants are kept since
    # different SCE exports use different ones.
    "course_name": ["שם הקורס", "שם קורס", "שם המקצוע", "שם שיעור", "שם השיעור", "שם שעור", "מקצוע", "קורס",
                    "course name", "course title", "subject", "course"],
    # "ת.בחינה" is SCE's abbreviated "תאריך בחינה" header (confirmed against a
    # real exam-schedule PDF); the bare "תאריך" keyword never matches it.
    "date": ["תאריך הבחינה", "תאריך מבחן", "יום ותאריך", "תאריך", "ת.בחינה", "ת. בחינה", "exam date", "date"],
    # end_time before start_time: "time" (start_time's generic fallback) is
    # a substring of "end time", the same trap as שעה inside "עד שעה".
    "end_time": ["עד שעה", "שעת סיום", "שעה עד", "סיום", "end time", "until", "finish"],
    "start_time": ["משעה", "שעת התחלה", "שעה מ", "שעה", "start time", "start", "from", "time", "hour"],
    "room": ["חדר", "אולם", "מיקום", "בניין", "room", "hall", "location", "building", "classroom"],
    "moed": ["מועד", "moed"],
}

DATE_RE = re.compile(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")
HEBREW_LETTER_RE = re.compile("[֐-׿]")
RUN_RE = re.compile(r"\d+|\D+")

MIN_HEADER_KEYWORD_HITS = 2

# Real exam-schedule/grade-sheet/class-schedule exports are simple
# text-based PDFs, typically well under 1MB. 20MB is a generous ceiling that
# rejects abusive/oversized uploads before they reach pdfplumber (the
# expensive parsing step) without affecting any legitimate file.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Real PDFs start with this signature (some generators emit a few bytes of
# leading noise/BOM before it, which is why real PDF readers -- and this
# check -- tolerate it anywhere in the first 1024 bytes rather than
# requiring it at byte 0). The filename/Content-Type each endpoint also
# checks are both entirely client-controlled and trivially spoofed (e.g.
# renaming any file to *.pdf); this looks at the actual bytes instead.
PDF_MAGIC = b"%PDF-"
PDF_MAGIC_SEARCH_WINDOW = 1024


def validate_pdf_contents(contents: bytes) -> None:
    if not contents:
        raise HTTPException(status_code=400, detail="הקובץ ריק.")
    if PDF_MAGIC not in contents[:PDF_MAGIC_SEARCH_WINDOW]:
        raise HTTPException(status_code=400, detail="הקובץ שהועלה אינו PDF תקין.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="הקובץ גדול מדי (מקסימום 20MB).")


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


MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "1 Feb 2026" / "Feb 1, 2026" -- English exports write dates as text, not
# only as DD/MM/YYYY. Only the first three letters of the month matter, so
# both "Feb" and "February" match.
TEXT_DATE_DMY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})")
TEXT_DATE_MDY_RE = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")


def normalize_date(raw: str) -> Optional[str]:
    """DD/MM/YYYY (or . / - separated, 2- or 4-digit year), '1 Feb 2026' or
    'Feb 1, 2026' -> ISO yyyy-mm-dd."""
    day = month = year_num = None
    match = DATE_RE.search(raw)
    if match:
        day, month, year = match.groups()
        day, month, year_num = int(day), int(month), int(year)
        if year_num < 100:
            year_num += 2000
    else:
        m = TEXT_DATE_DMY_RE.search(raw)
        if m and m.group(2)[:3].lower() in MONTH_NAMES:
            day, month, year_num = int(m.group(1)), MONTH_NAMES[m.group(2)[:3].lower()], int(m.group(3))
        else:
            m = TEXT_DATE_MDY_RE.search(raw)
            if m and m.group(1)[:3].lower() in MONTH_NAMES:
                day, month, year_num = int(m.group(2)), MONTH_NAMES[m.group(1)[:3].lower()], int(m.group(3))
    if day is None:
        return None
    try:
        return datetime(year_num, month, day).date().isoformat()
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
    """Substring match, ignoring whitespace on both sides: pdfplumber breaks
    a wrapped header into "מס.שיעור\nוקבוצה" and detaches Hebrew final
    letters ("ה ניחב.ת" reversed is "ת.בחינ ה"), so "ת.בחינה" would never
    match the cell as extracted."""
    if not header_cell:
        return None
    squashed = re.sub(r"\s+", "", header_cell.lower())
    for field, keywords in FIELD_KEYWORDS.items():
        for keyword in keywords:
            if re.sub(r"\s+", "", keyword) in squashed:
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


# Final forms plus ת: the letters pdfplumber was seen detaching from the end
# of a word ("הנדסי ת", "פרויקטי ם"). Deliberately not every letter -- a
# trailing "א"/"ב" is a real group/part marker ("פיסיקה 2 ב") and must stay.
FINAL_LETTER_GAP_RE = re.compile(r"(?<=[א-ת]) ([ךםןףץת])(?=\s|$)")


def _rejoin_final_letters(text: str) -> str:
    """pdfplumber sometimes extracts a word's last letter with a space
    before it ("אמינות הנדסי ת"); no Hebrew word is a lone final letter."""
    return FINAL_LETTER_GAP_RE.sub(r"\1", text)


def _only_match(row, reverse: bool, pattern, convert):
    """The single cell in `row` matching `pattern`, converted; None when
    zero or several cells match (several = ambiguous, leave it alone)."""
    hits = []
    for raw in row:
        value = normalize_cell(raw, reverse)
        if value and pattern.search(value):
            converted = convert(value)
            if converted:
                hits.append(converted)
    return hits[0] if len(hits) == 1 else None


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

        course_name = _rejoin_final_letters(cell(row, "course_name"))
        if not course_name:
            continue

        # A multi-row header (e.g. "החדר בו / תתקיים / הבחינה" stacked over
        # one column) can shift pdfplumber's header cells one slot away
        # from the body cells beneath them -- confirmed on a real SCE exam
        # schedule, where the date header landed one column left of every
        # date. When the mapped cell holds no date, take the row's only
        # date-shaped cell instead; a row with two candidates stays
        # ambiguous and is skipped as before.
        date_iso = normalize_date(cell(row, "date"))
        if not date_iso:
            date_iso = _only_match(row, reverse, DATE_RE, normalize_date)
        if not date_iso:
            continue

        start_raw = cell(row, "start_time")
        if not normalize_time(start_raw):
            start_raw = _only_match(row, reverse, TIME_RE, lambda s: s) or start_raw
        end_raw = cell(row, "end_time")
        start_time = normalize_time(start_raw) if start_raw else None
        end_time = normalize_time(end_raw) if end_raw else None

        # A single time column sometimes holds a full "HH:MM-HH:MM" range
        # (common when the source PDF only exports one time column).
        if start_time and not end_time:
            range_start, range_end = extract_time_range(start_raw)
            if range_start:
                start_time, end_time = range_start, range_end

        # RTL extraction can flip the visual order of a time pair (the
        # digits themselves survive bidi intact but the two times swap
        # places). Exams never span midnight, so start > end is always the
        # flipped case, never a real range.
        if start_time and end_time and start_time > end_time:
            start_time, end_time = end_time, start_time

        record = {
            "course_name": course_name,
            "date": date_iso,
            "start_time": start_time,
            "end_time": end_time,
        }
        room = cell(row, "room")
        if room and re.search(r"\w", room):  # "*" / "-" placeholders carry nothing
            record["room"] = room
        moed = cell(row, "moed")
        if moed:
            record["moed"] = moed
        course_code = cell(row, "course_code")
        if course_code:
            record["course_code"] = course_code

        records.append(record)

    return records


# --- Free-text-line fallback for exam schedules ---
# Last-resort layer for exam-schedule PDFs where no table can be recovered
# at all (no ruling lines AND column alignment too irregular for the
# text-strategy pass): treat each text line carrying a date + a time as a
# candidate exam row. Fuzzier than column mapping by design -- the frontend
# shows every parsed row in a review table before anything is imported, so
# an occasional stray row is visible and deletable there, whereas the
# previous behavior (hard 422, zero rows) left the user with nothing.

# Lines that carry a date+time but are document chrome, not exam rows
# (print footers, "data current as of" stamps, page numbers).
METADATA_LINE_RE = re.compile(r"הדפסה|הודפס|עמוד\s*\d|נכון ל|(?i:printed|page\s*\d|as of|generated)")

# A course name is real when it has a few letters in either alphabet --
# the fallback must not require Hebrew, or English schedules never parse.
NAME_LETTER_RE = re.compile(r"[A-Za-z֐-׿]")
MOED_TOKEN_RE = re.compile(r"מועד\s*ה?בחינה|מועד\s*([אבג])['׳]?")
# Standalone digit/punctuation tokens left over after stripping date+times
# (course codes, room numbers, row indices) -- not part of the course name.
NUMERIC_TOKEN_RE = re.compile(r"^[\d\-./:()]+$")
COURSE_CODE_TOKEN_RE = re.compile(r"^\d{5,8}$")

# Words common in real Israeli schedule/transcript exports, used to decide
# whether a page's extracted text needs the bidi fix (see fix_bidi_line):
# whichever orientation contains more of these words is the readable one.
# Header-keyword scoring can't be reused here because this fallback runs
# exactly when no header row was found.
_COMMON_HEBREW_WORDS = ("מועד", "בחינה", "מבחן", "קורס", "סמסטר", "תאריך", "שעה", "חדר")


def _page_needs_reverse(text: str) -> bool:
    fixed = "\n".join(fix_bidi_line(line) for line in text.split("\n"))
    raw_score = sum(text.count(w) for w in _COMMON_HEBREW_WORDS)
    fixed_score = sum(fixed.count(w) for w in _COMMON_HEBREW_WORDS)
    return fixed_score > raw_score


def parse_exam_text_lines(page) -> list:
    text = page.extract_text() or ""
    if not text.strip():
        return []
    needs_reverse = _page_needs_reverse(text)

    records = []
    for raw_line in text.split("\n"):
        line = fix_bidi_line(raw_line) if needs_reverse else raw_line
        line = re.sub(r"\s+", " ", line).strip()
        if not line or METADATA_LINE_RE.search(line):
            continue

        date_iso = normalize_date(line)
        if not date_iso:
            continue
        # Requiring a time as well filters out most non-exam dated lines
        # (semester headers, signature dates) at the cost of dropping the
        # rare schedule that omits times -- the safer trade for a fallback.
        times = sorted({f"{int(h):02d}:{int(m):02d}" for h, m in TIME_RE.findall(line)
                        if int(h) <= 23 and int(m) <= 59})
        if not times:
            continue

        # Strip whichever date form actually matched. Text-date stripping
        # runs ONLY when no numeric date exists and ONLY on real month names,
        # because its pattern ("word digits year-like-number") would
        # otherwise eat legitimate name text such as "Physics 2 1234".
        if DATE_RE.search(line):
            remainder = DATE_RE.sub(" ", line)
        else:
            remainder = TEXT_DATE_DMY_RE.sub(
                lambda m: " " if m.group(2)[:3].lower() in MONTH_NAMES else m.group(0), line)
            remainder = TEXT_DATE_MDY_RE.sub(
                lambda m: " " if m.group(1)[:3].lower() in MONTH_NAMES else m.group(0), remainder)
        remainder = TIME_RE.sub(" ", remainder)

        moed = None
        moed_match = MOED_TOKEN_RE.search(remainder)
        if moed_match:
            moed = ("מועד " + moed_match.group(1)) if moed_match.group(1) else None
            remainder = MOED_TOKEN_RE.sub(" ", remainder)

        course_code = None
        name_tokens = []
        prev_was_name = False
        for token in remainder.split():
            if COURSE_CODE_TOKEN_RE.match(token):
                if course_code is None:
                    course_code = token
                prev_was_name = False
                continue
            if NUMERIC_TOKEN_RE.match(token):
                # A lone digit right after a name word is part of the name
                # ("פיזיקה 1", "אלגברה 2") -- longer numeric runs, and digits
                # not adjacent to a name word (row indices, room numbers),
                # are not.
                if prev_was_name and len(token) == 1 and token.isdigit():
                    name_tokens.append(token)
                prev_was_name = False
                continue
            name_tokens.append(token)
            prev_was_name = True
        course_name = " ".join(name_tokens).strip(" -.,:;|")

        # A real course name has a few letters (Hebrew or Latin); anything
        # shorter is residue (a stray moed letter, a building abbreviation).
        if len(NAME_LETTER_RE.findall(course_name)) < 3:
            continue

        record = {
            "course_name": course_name,
            "date": date_iso,
            "start_time": times[0],
            "end_time": times[1] if len(times) > 1 else None,
        }
        if moed:
            record["moed"] = moed
        if course_code:
            record["course_code"] = course_code
        records.append(record)

    return records


# Grade-sheet (transcript) parsing -- separate field-keyword map from the
# exam-schedule one above since a grade sheet's columns (course/grade/
# credits) don't overlap with a schedule's (course/date/time/room).
# match_grade_field checks FIELDS in this dict's order and a field's keyword
# match is a plain substring test -- so exam_grade (specific phrases like
# "ציון מבחן") MUST be listed before grade (whose fallback bare "ציון" is
# itself a substring of "ציון מבחן"), or the generic "grade" field would
# steal the exam-grade column before exam_grade's own keywords are ever
# tried. course_name is checked first regardless since its keywords don't
# overlap with either grade field's.
GRADE_FIELD_KEYWORDS = {
    # credits MUST precede course_code: course_code's bare "קוד" fallback is
    # a substring of "נקודות" ("נקודות זכות" contains קוד), so with the
    # reverse order a credits column is silently absorbed as a second
    # course-code match and never mapped -- caught by the synthetic-PDF
    # suite the day course_code was added to this map.
    "credits": ["נ.זיכוי", "נקודות זיכוי", "נקודות זכות", 'נ"ז', "זיכוי", "זכות", "credits", "credit", "ects"],
    # Before course_name so a "Course Code"/"קוד קורס" column is absorbed
    # here and never claimed by course_name's generic "קורס"/"course"
    # fallback (same cross-field ordering trap as in FIELD_KEYWORDS above).
    # The value is also carried into the record when present.
    "course_code": ["קוד קורס", "מספר קורס", "מס' קורס", "קוד", "course code", "course no", "course id", "code"],
    # "שם שעור" (no yud) is SCE college's own spelling on its grade-sheet
    # (transcript) export -- distinct from the "שם שיעור" spelling used on
    # SCE's exam-schedule export (see FIELD_KEYWORDS above). Confirmed
    # against a real SCE transcript PDF where this was the only column-header
    # variant present, so without it course_name never matched and the whole
    # sheet was rejected as "no grade table found".
    "course_name": ["שם הקורס", "שם קורס", "שם השיעור", "שם שיעור", "שם שעור", "מקצוע", "קורס",
                    "course name", "course title", "subject", "course"],
    "exam_grade": ["ציון מבחן", "ציון בחינה", "ציון בכתב", "exam grade", "exam score", "test grade"],
    # "ציון סופי" (final grade) -- listed before the bare "ציון" fallback for
    # the same substring reason as above, applied within this field's own
    # list; likewise "final grade" before the bare "grade".
    "grade": ["ציון סופי", "ציון כולל", "ציון", "final grade", "final score", "grade", "mark", "score"],
    "label": ["סמסטר", "תקופה", "מועד", "שנת לימודים", "semester", "term", "period", "year"],
}

GRADE_RE = re.compile(r"^(\d{1,3}(?:\.\d+)?)$")


def match_grade_field(header_cell: str) -> Optional[str]:
    if not header_cell:
        return None
    lowered = header_cell.lower()
    for field, keywords in GRADE_FIELD_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                return field
    return None


def detect_grade_header_row(table):
    """Same approach as detect_header_row (bidi-reversal detected per-table,
    best-scoring row within the first few), against GRADE_FIELD_KEYWORDS."""
    best_idx, best_score, best_reverse = -1, 0, False
    for idx, row in enumerate(table[:5]):
        for reverse in (False, True):
            score = sum(1 for cell in row if match_grade_field(normalize_cell(cell, reverse)))
            if score > best_score:
                best_idx, best_score, best_reverse = idx, score, reverse
    if best_score < MIN_HEADER_KEYWORD_HITS:
        return -1, False
    return best_idx, best_reverse


def map_grade_columns(header_row, reverse: bool) -> dict:
    mapping = {}
    for idx, raw_cell in enumerate(header_row):
        field = match_grade_field(normalize_cell(raw_cell, reverse))
        if field and field not in mapping:
            mapping[field] = idx
    return mapping


def normalize_grade(raw: str) -> Optional[float]:
    """Grades are 0-100 -- reject anything outside that range rather than
    silently accepting a stray credits/year number from a misaligned column."""
    if not raw:
        return None
    s = raw.strip().replace(",", ".")
    match = GRADE_RE.match(s)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 100 else None


def parse_grade_table(table) -> list:
    if not table:
        return []
    header_idx, reverse = detect_grade_header_row(table)
    if header_idx == -1:
        return []
    mapping = map_grade_columns(table[header_idx], reverse)
    if "course_name" not in mapping or "grade" not in mapping:
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
        grade = normalize_grade(cell(row, "grade"))
        if not course_name or grade is None:
            continue

        record = {"course_name": course_name, "grade": grade}
        exam_grade = normalize_grade(cell(row, "exam_grade"))
        if exam_grade is not None:
            record["exam_grade"] = exam_grade
        credits_raw = cell(row, "credits")
        if credits_raw:
            try:
                record["credits"] = float(credits_raw.replace(",", "."))
            except ValueError:
                pass
        label = cell(row, "label")
        if label:
            record["label"] = label
        course_code = cell(row, "course_code")
        if course_code:
            record["course_code"] = course_code

        records.append(record)

    return records


DAY_NAME_TO_IDX = {
    "ראשון": 0, "שני": 1, "שלישי": 2, "רביעי": 3, "חמישי": 4, "שישי": 5, "שבת": 6,
    # English day headers, matched case-insensitively via _day_index below.
    "sunday": 0, "sun": 0, "monday": 1, "mon": 1, "tuesday": 2, "tue": 2,
    "wednesday": 3, "wed": 3, "thursday": 4, "thu": 4, "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}


def _day_index(word: str) -> Optional[int]:
    return DAY_NAME_TO_IDX.get(word) if word in DAY_NAME_TO_IDX else DAY_NAME_TO_IDX.get(word.lower())
TIME_LABEL_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
COURSE_CODE_RE = re.compile(r"\b(\d{6,7}-\d{1,2})\b")
ROOM_NUM_RE = re.compile(r"^\d{3,5}$")
BUILDING_RE = re.compile(r"בניין|בנין")
METADATA_RE = re.compile(r'ש["״]ש|נ["״]ז')
LECTURER_TITLE_RE = re.compile(
    r'^(פרופ|ד["״]ר|דר|מר |גב["׳\'׳]|גברת|מהנדס|עו["״]ד'
    r'|(?i:prof|dr|mr|mrs|ms|eng)\.?\s)'
)


def _label_to_minutes(label: str) -> int:
    """'09:30' -> 570. Only ever called on labels TIME_LABEL_RE already matched."""
    hh, mm = label.split(":")
    return int(hh) * 60 + int(mm)


def _median(nums):
    s = sorted(nums)
    n = len(s)
    if n == 0:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _cell_lines(page, rect):
    """Read a class cell's text as clean visual lines. Some classes in a real
    schedule sit flush against their neighbour (zero gap between rect
    boundaries -- confirmed against a real Sapir College export), so this
    filters page.chars to EXACTLY the cell's own rect using a half-open
    interval on each char's center point ([top, bottom), [x0, x1)): every
    char belongs to exactly one cell by construction, with no shared-border
    double-membership the way a padded crop has. pdfminer's own layout
    extraction on that exact char set then reconstructs multi-word Hebrew
    lines far more reliably than joining pdfplumber's per-word tokens."""
    top, bottom, x0, x1 = rect["top"], rect["bottom"], rect["x0"], rect["x1"]

    def in_cell(obj):
        cy = (obj.get("top", 0) + obj.get("bottom", 0)) / 2
        cx = (obj.get("x0", 0) + obj.get("x1", 0)) / 2
        return top <= cy < bottom and x0 <= cx < x1

    try:
        raw = page.filter(in_cell).extract_text() or ""
    except Exception:
        raw = ""
    return [fix_bidi_line(ln.strip()) for ln in raw.split("\n") if ln.strip()]


def parse_class_schedule(page):
    """Parse a weekly-calendar-grid timetable (days across columns, half-hour
    rows, each class a bordered cell). Returns records or None if the page is
    not a recognizable grid schedule.

    Geometry is derived entirely from the PDF's own day-header words, time
    labels, and class-cell rectangles -- no hardcoded coordinates -- so it
    generalizes across institutions that export this calendar layout."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)

    # 1. Day-header row: words that are day names give each day's x-center.
    day_centers = []  # (x_center, day_idx)
    header_bottom = 0
    for w in words:
        t = fix_bidi_line(w["text"]).strip()
        day_idx = _day_index(t)
        if day_idx is not None:
            day_centers.append(((w["x0"] + w["x1"]) / 2, day_idx))
            header_bottom = max(header_bottom, w["bottom"])
    if len(day_centers) < 3:
        return None

    def nearest_day(xc):
        return min(day_centers, key=lambda p: abs(p[0] - xc))[1]

    # Day-column width from the spacing between adjacent day centers.
    xs = sorted(c for c, _ in day_centers)
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    col_w = _median(gaps) if gaps else 80

    # 2. Time labels (below the header) -> discrete y->time snap points.
    raw_labels = []  # (top, "HH:MM", x_center)
    for w in words:
        m = TIME_LABEL_RE.match(w["text"].strip())
        if m and w["top"] > header_bottom - 6:
            hh, mm = int(m.group(1)), int(m.group(2))
            if hh <= 23 and mm <= 59:
                raw_labels.append((w["top"], f"{hh:02d}:{mm:02d}", (w["x0"] + w["x1"]) / 2))
    if len(raw_labels) < 2:
        return None
    raw_labels.sort()
    # Keep only the main regularly-spaced ladder: drop outliers like the footer
    # print-time ("שעת הדפסה : 19:16") that sits far below the last real row.
    row_gap = _median([raw_labels[i + 1][0] - raw_labels[i][0] for i in range(len(raw_labels) - 1)])
    kept = [raw_labels[0]]
    for prev, cur in zip(raw_labels, raw_labels[1:]):
        if cur[0] - kept[-1][0] <= max(row_gap * 3, 30):
            kept.append(cur)
    time_labels = [(t, s) for t, s, _ in kept]
    time_col_center = _median([x for _, _, x in kept])
    grid_bottom = time_labels[-1][0] + max(row_gap, 12)

    # The hour column only names whole rows (30 minutes at SCE), but not every
    # institution draws its class blocks on those lines: Sapir puts them on HALF
    # rows too, i.e. quarter past and quarter to. Snapping such a block to the
    # nearest *printed* label silently pulls it 15 minutes earlier -- measured on
    # a real Sapir timetable, 3 of 10 classes came out a quarter of an hour early
    # with no warning of any kind. So the ladder gets the midpoints added to it.
    # Nearest-match semantics are kept deliberately: block edges sit a couple of
    # points off the label they belong to, and matching by distance absorbs that
    # offset, where interpolating from label coordinates would not.
    snap_points = []  # (y, minutes)
    for (y_a, label_a), (y_b, label_b) in zip(time_labels, time_labels[1:]):
        m_a, m_b = _label_to_minutes(label_a), _label_to_minutes(label_b)
        snap_points.append((y_a, m_a))
        if m_b - m_a > 0:
            snap_points.append(((y_a + y_b) / 2.0, (m_a + m_b) // 2))
    snap_points.append((time_labels[-1][0], _label_to_minutes(time_labels[-1][1])))

    def snap_time(y):
        minutes = min(snap_points, key=lambda p: abs(p[0] - y))[1]
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # 3. Class cells: rectangles below the header, one day-column wide, sitting
    # in a day column (not the time column).
    class_rects = []
    for r in page.rects:
        if r["top"] < header_bottom or r["top"] > grid_bottom or r["height"] < 6:
            continue
        if not (0.5 * col_w < r["width"] < 1.5 * col_w):
            continue
        xc = (r["x0"] + r["x1"]) / 2
        if abs(xc - time_col_center) < col_w * 0.5:
            continue  # the time column itself
        day = nearest_day(xc)
        # Only accept if the rect actually lines up with a day center.
        if min(abs(xc - c) for c, _ in day_centers) > col_w * 0.6:
            continue
        class_rects.append((r, day))
    if not class_rects:
        return None

    # 4. For each class cell, read its own lines and classify them.
    records = []
    for r, day in class_rects:
        lines = _cell_lines(page, r)
        if not lines:
            continue

        start_time = snap_time(r["top"])
        end_time = snap_time(r["bottom"])

        lecturer = ""
        room_num = ""
        building = ""
        course_code = ""
        credits = None
        name_lines = []
        seen_lecturer = False

        for text in lines:
            if not text:
                continue
            code_m = COURSE_CODE_RE.search(text)
            if code_m and not course_code:
                course_code = code_m.group(1)
                continue
            if METADATA_RE.search(text):
                cm = re.search(r'נ["״]ז[:\s]*([0-9]+)', text)
                if cm and credits is None:
                    credits = int(cm.group(1))
                continue
            if ROOM_NUM_RE.match(text):
                if not room_num:
                    room_num = text
                continue
            if BUILDING_RE.search(text):
                if not building:
                    building = text
                continue
            if LECTURER_TITLE_RE.match(text):
                if not lecturer:
                    lecturer = text
                seen_lecturer = True
                continue
            # Otherwise it's course-name text (only before the lecturer line).
            if not seen_lecturer:
                name_lines.append(text)

        course_name = " ".join(name_lines).strip()
        location = " ".join(x for x in (building, room_num) if x).strip()

        records.append({
            "course_name": course_name,
            "day": day,
            "start_time": start_time,
            "end_time": end_time,
            "lecturer": lecturer,
            "room": location,
            "course_code": course_code,
            "credits": credits if credits is not None else "",
        })

    records.sort(key=lambda c: (c["day"], c["start_time"]))
    return records


# --- Row-per-class table layout for class schedules (מערכת שעות) ---
# parse_class_schedule above reads the weekly-calendar-grid layout (days
# across columns, each class a bordered cell). Several institutions export
# the same schedule as a plain table instead -- one row per class with
# day / from-hour / to-hour / course code / course name / credits / weekly
# hours / lecturer / room columns (confirmed against a real SCE export,
# whose grid detection found no day-header row at all and the whole file
# was rejected as "no schedule recognized"). Same header-keyword approach
# as FIELD_KEYWORDS/GRADE_FIELD_KEYWORDS, with the same two ordering
# sensitivities: within a field, specific phrases before generic ones, and
# ACROSS fields because match_class_field checks fields in this dict's own
# order and returns on the first substring hit.
CLASS_FIELD_KEYWORDS = {
    # end_time first of the time-ish fields: "שעה" (start_time's generic
    # fallback) is a substring of "עד שעה", and "יום" (day's keyword) is a
    # substring of "סיום" -- so end_time must be tried before both
    # start_time and day can see the cell.
    "end_time": ["עד שעה", "שעה עד", "שעת סיום", "סיום", "end time", "until", "finish"],
    # A weekly-hours column ("שעות", SCE's ש"ש) would otherwise be claimed
    # by start_time's bare "שעה" fallback -- absorbed here and then simply
    # not carried into the record.
    "hours": ["שעות", 'ש"ש', "ש״ש", "weekly hours", "hours"],
    "start_time": ["משעה", "שעת התחלה", "שעה מ", "התחלה", "שעה", "start time", "start", "from", "hour"],
    "day": ["יום", "day"],
    # credits before course_code: "קוד" is a substring of "נקודות" (the
    # same trap documented on GRADE_FIELD_KEYWORDS above).
    "credits": ["נ.זיכוי", "נקודות זיכוי", "נקודות זכות", 'נ"ז', "נ״ז", "זיכוי", "credits", "credit", "ects"],
    # course_code before course_name: course_name's generic "שיעור"/"קורס"
    # fallbacks are substrings of "קוד שיעור"/"קוד קורס".
    "course_code": ["קוד שיעור", "קוד קורס", "מספר קורס", "מס' קורס", "קוד", "course code", "course no", "course id", "code"],
    "course_name": ["שם שיעור", "שם השיעור", "שם שעור", "שם הקורס", "שם קורס", "שם מקצוע", "מקצוע", "שיעור", "קורס",
                    "course name", "course title", "subject", "course"],
    "lecturer": ["מרצה", "lecturer", "instructor", "teacher"],
    "room": ["חדר", "כיתה", "אולם", "מיקום", "בניין", "room", "hall", "location", "classroom", "building"],
}

# Single-letter day cells ("א".."ו", "ש" for Saturday) -- how the SCE table
# export writes its day column. Full day names go through _day_index.
CLASS_DAY_LETTERS = {"א": 0, "ב": 1, "ג": 2, "ד": 3, "ה": 4, "ו": 5, "ש": 6}
DAY_PREFIX_RE = re.compile(r"^יום\s*")


def parse_class_day(raw: str) -> Optional[int]:
    if not raw:
        return None
    text = DAY_PREFIX_RE.sub("", raw.strip()).strip(" '׳´`\"”.")
    idx = _day_index(text)
    if idx is not None:
        return idx
    return CLASS_DAY_LETTERS.get(text)


def match_class_field(header_cell: str) -> Optional[str]:
    if not header_cell:
        return None
    lowered = header_cell.lower()
    for field, keywords in CLASS_FIELD_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                return field
    return None


def detect_class_header_row(table):
    """Same approach as detect_header_row (bidi-reversal detected per-table,
    best-scoring row within the first few), against CLASS_FIELD_KEYWORDS."""
    best_idx, best_score, best_reverse = -1, 0, False
    for idx, row in enumerate(table[:5]):
        for reverse in (False, True):
            score = sum(1 for cell in row if match_class_field(normalize_cell(cell, reverse)))
            if score > best_score:
                best_idx, best_score, best_reverse = idx, score, reverse
    if best_score < MIN_HEADER_KEYWORD_HITS:
        return -1, False
    return best_idx, best_reverse


def map_class_columns(header_row, reverse: bool) -> dict:
    mapping = {}
    for idx, raw_cell in enumerate(header_row):
        field = match_class_field(normalize_cell(raw_cell, reverse))
        if field and field not in mapping:
            mapping[field] = idx
    return mapping


def parse_class_table(table) -> list:
    """Row-per-class table -> the same record shape parse_class_schedule
    produces, so the frontend review table needs no translation."""
    if not table:
        return []
    header_idx, reverse = detect_class_header_row(table)
    if header_idx == -1:
        return []
    mapping = map_class_columns(table[header_idx], reverse)
    # Without a day and a start time this is some other course table (an
    # exam schedule, a transcript), not a weekly class schedule.
    if "course_name" not in mapping or "day" not in mapping or "start_time" not in mapping:
        return []

    def cell(row, field):
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        return normalize_cell(row[idx], reverse)

    records = []
    last_day = None
    for row in table[header_idx + 1:]:
        if row is None or all(not normalize_cell(c) for c in row):
            continue

        course_name = cell(row, "course_name")
        if not course_name:
            continue

        # A merged/repeated day cell may extract as empty on continuation
        # rows -- rows are grouped by day, so the previous row's day holds.
        day = parse_class_day(cell(row, "day"))
        if day is None:
            day = last_day
        if day is None:
            continue
        last_day = day

        start_time = normalize_time(cell(row, "start_time"))
        end_time = normalize_time(cell(row, "end_time"))
        # Same RTL-flip safety as parse_table: classes never span midnight,
        # so start > end is always the flipped case, never a real range.
        if start_time and end_time and start_time > end_time:
            start_time, end_time = end_time, start_time

        credits = None
        credits_raw = cell(row, "credits")
        if credits_raw:
            try:
                credits = float(credits_raw.replace(",", "."))
            except ValueError:
                pass

        records.append({
            "course_name": course_name,
            "day": day,
            "start_time": start_time,
            "end_time": end_time,
            "lecturer": cell(row, "lecturer"),
            "room": cell(row, "room"),
            "course_code": cell(row, "course_code"),
            "credits": credits if credits is not None else "",
        })

    records.sort(key=lambda c: (c["day"], c["start_time"] or ""))
    return records


# Generous enough for a real multi-page schedule/transcript, but bounded so
# one adversarial or pathologically slow PDF can't hang a request forever.
PARSE_TIMEOUT_SECONDS = 30

# A digital exam-schedule/transcript export always carries at least a few
# dozen extractable characters (headers alone exceed this). Below it, the
# document is a scan/photo (image-only pages) -- text extraction is
# structurally impossible, and the user needs to be told THAT rather than
# the generic "no table recognized" (which reads as "try a different
# table"), because no re-upload of the same scan can ever succeed.
MIN_EXTRACTABLE_TEXT_CHARS = 20

# Tried on tables when the default (ruling-lines-based) pass finds nothing:
# many institutions export schedules as visually aligned text columns with
# no drawn cell borders, which the default strategy cannot see at all.
# Word-alignment-based detection is noisier, but every candidate table it
# yields still has to pass the header-keyword gate (MIN_HEADER_KEYWORD_HITS
# + required-column checks), so a garbage grid is rejected the same way any
# non-schedule table is.
TEXT_TABLE_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def _extract_records_sync(contents: bytes, strategies: list) -> list:
    """The actual pdfplumber work -- synchronous and CPU/IO-bound, so this
    must never be awaited directly in an endpoint (see the run_in_threadpool
    calls below). Previously it ran straight in the event loop, meaning a
    single slow/malicious PDF blocked ALL requests this process was
    handling, not just its own -- with the render.yaml deploy's single
    worker, that meant one bad upload could stall the entire service.
    Offloading to a thread pool means other requests keep being served
    while this one runs; wrapping the caller in asyncio.wait_for (below)
    additionally bounds how long a client waits before getting a clean
    error instead of hanging indefinitely. Note this bounds the CLIENT's
    wait, not the underlying thread itself -- Python can't forcibly kill a
    running thread, so a genuinely pathological file still finishes
    consuming its thread-pool slot in the background after the timeout
    fires; a full fix would need a subprocess-based sandbox, which is a
    much larger architectural change than this warrants.

    `strategies` is an ordered list of per-page extractors, best-quality
    first. The first strategy that yields ANY records for the document wins
    outright -- later (fuzzier) strategies are fallbacks for documents the
    precise ones can't read at all, not supplements, since the same rows
    would otherwise be extracted twice by two different strategies. If every
    strategy comes up empty on a document with no extractable text, the
    document is a scan -- reported as its own distinct error (see
    MIN_EXTRACTABLE_TEXT_CHARS)."""
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for page_records in strategies:
            records = []
            for page in pdf.pages:
                records.extend(page_records(page))
            if records:
                return records
        total_text = "".join((page.extract_text() or "") for page in pdf.pages)
        if len(total_text.strip()) < MIN_EXTRACTABLE_TEXT_CHARS:
            logger.warning("PDF appears to be a scan: no extractable text")
            raise HTTPException(
                status_code=422,
                detail="הקובץ סרוק כתמונה ולא ניתן לקרוא ממנו טקסט. נסה להוריד גרסה דיגיטלית של הקובץ מאתר המוסד במקום סריקה או צילום.",
            )
        return []


async def _parse_with_timeout(contents: bytes, strategies) -> list:
    if callable(strategies):
        strategies = [strategies]
    try:
        return await asyncio.wait_for(
            run_in_threadpool(_extract_records_sync, contents, strategies),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("PDF parsing timed out after %s seconds", PARSE_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="ניתוח הקובץ ארך זמן רב מדי. נסה קובץ קטן או פשוט יותר.",
        )
    except HTTPException:
        raise
    except Exception:  # pdfplumber/pdfminer can raise many exception types on malformed PDFs
        logger.exception("PDF parsing failed")
        raise HTTPException(status_code=422, detail="קריאת ה-PDF נכשלה. ודא שזהו קובץ PDF תקין ונסה שוב.")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse-pdf")
async def parse_pdf(file: UploadFile = File(...)):
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="הקובץ שהועלה אינו PDF.")

    contents = await file.read()
    validate_pdf_contents(contents)

    # Best-quality first: bordered tables, then borderless (text-aligned)
    # tables, then raw text lines. See _extract_records_sync for why only
    # the first non-empty layer's records are used.
    strategies = [
        lambda page: [r for table in page.extract_tables() for r in parse_table(table)],
        lambda page: [r for table in page.extract_tables(TEXT_TABLE_SETTINGS) for r in parse_table(table)],
        parse_exam_text_lines,
    ]

    records = await _parse_with_timeout(contents, strategies)

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהתה טבלת בחינות בקובץ. ודא שהקובץ מכיל טבלה עם כותרות בעברית (למשל: קורס, תאריך, שעה).",
        )

    return records


@app.post("/parse-grade-sheet")
async def parse_grade_sheet(file: UploadFile = File(...)):
    """Grade sheet / transcript PDF -> JSON rows: course_name, grade (final,
    authoritative), and optional exam_grade/credits/label -- same shape the
    frontend's gradeSheetParser.js already produces from CSV/Excel, so it
    slots into the same review table without any shape translation."""
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="הקובץ שהועלה אינו PDF.")

    contents = await file.read()
    validate_pdf_contents(contents)

    # No free-text fallback here: a grade line is just a name plus bare
    # numbers, and guessing which number is the grade vs credits vs course
    # code without a header column is exactly the kind of wrong-but-
    # plausible data that shouldn't reach a student's transcript.
    strategies = [
        lambda page: [r for table in page.extract_tables() for r in parse_grade_table(table)],
        lambda page: [r for table in page.extract_tables(TEXT_TABLE_SETTINGS) for r in parse_grade_table(table)],
    ]

    records = await _parse_with_timeout(contents, strategies)

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהה גליון ציונים בקובץ. ודא שהקובץ מכיל טבלה עם כותרות בעברית (למשל: שם קורס, ציון סופי).",
        )

    return records


@app.post("/parse-class-schedule-pdf")
async def parse_class_schedule_pdf(file: UploadFile = File(...)):
    """Weekly class-schedule (מערכת שעות) grid PDF -> JSON matching the shape
    the React frontend's hebrewScheduleParser.js already produces, so it
    slots into the exact same review table without any shape translation:
    course_name, day (0=Sunday..6=Saturday), start_time, end_time, lecturer,
    room, course_code, credits."""
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf") and file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="הקובץ שהועלה אינו PDF.")

    contents = await file.read()
    validate_pdf_contents(contents)

    # Best-quality first, same layering idea as /parse-pdf: the geometric
    # grid parser (which returns None -- normalized to [] -- for a page
    # that isn't a grid schedule), then row-per-class tables with drawn
    # borders, then borderless (text-aligned) tables. Only the first
    # non-empty layer's records are used -- see _extract_records_sync.
    strategies = [
        lambda page: parse_class_schedule(page) or [],
        lambda page: [r for table in page.extract_tables() for r in parse_class_table(table)],
        lambda page: [r for table in page.extract_tables(TEXT_TABLE_SETTINGS) for r in parse_class_table(table)],
    ]

    records = await _parse_with_timeout(contents, strategies)

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהתה מערכת שעות בקובץ. ודא שהקובץ הוא מערכת שעות שבועית במבנה יומן (ימים בעמודות, שעות בשורות).",
        )

    return records
