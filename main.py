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


DAY_NAME_TO_IDX = {
    "ראשון": 0, "שני": 1, "שלישי": 2, "רביעי": 3, "חמישי": 4, "שישי": 5, "שבת": 6,
}
TIME_LABEL_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
COURSE_CODE_RE = re.compile(r"\b(\d{6,7}-\d{1,2})\b")
ROOM_NUM_RE = re.compile(r"^\d{3,5}$")
BUILDING_RE = re.compile(r"בניין|בנין")
METADATA_RE = re.compile(r'ש["״]ש|נ["״]ז')
LECTURER_TITLE_RE = re.compile(r'^(פרופ|ד["״]ר|דר|מר |גב["׳\'׳]|גברת|מהנדס|עו["״]ד)')


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
        if t in DAY_NAME_TO_IDX:
            day_centers.append(((w["x0"] + w["x1"]) / 2, DAY_NAME_TO_IDX[t]))
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

    def snap_time(y):
        return min(time_labels, key=lambda p: abs(p[0] - y))[1]

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
                # a line that is only the code carries nothing else
                if ROOM_NUM_RE.match(text.replace(course_code, "").strip() or "x"):
                    pass
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
            "_bbox": (r["x0"], r["top"], r["x1"], r["bottom"]),
        })

    records.sort(key=lambda c: (c["day"], c["start_time"]))
    return records


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    # Deploy-verification marker: bump the string on every push so polling
    # scripts can confirm the live instance actually picked up new code,
    # instead of inferring it from parsing output (Render deploys on this
    # project have been taking much longer than usual this session, and
    # output-based inference produced false negatives).
    return {"marker": "grid-parser-v7-cellhalfopen"}


@app.post("/debug-parse-class")
async def debug_parse_class(file: UploadFile = File(...)):
    contents = await file.read()
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        page = pdf.pages[0]
        records = parse_class_schedule(page)
        for rec in records or []:
            x0, top, x1, bottom = rec["_bbox"]
            crop = page.crop((x0, top, x1, bottom))
            txt = crop.extract_text() or ""
            rec["_crop_lines"] = [fix_bidi_line(ln) for ln in txt.split("\n") if ln.strip()]
        return records


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
        rects = [
            {"x0": round(r["x0"], 1), "x1": round(r["x1"], 1), "top": round(r["top"], 1),
             "bottom": round(r["bottom"], 1), "w": round(r["width"], 1), "h": round(r["height"], 1)}
            for r in page.rects
        ]
        # Horizontal lines only, with their x-span, for cell-boundary detection.
        hlines = [
            {"top": round(ln["top"], 1), "x0": round(ln["x0"], 1), "x1": round(ln["x1"], 1)}
            for ln in page.lines if abs(ln["top"] - ln["bottom"]) < 1.0
        ]
        return {
            "page_width": round(page.width, 1),
            "page_height": round(page.height, 1),
            "num_words": len(out_words),
            "num_rects": len(rects),
            "num_hlines": len(hlines),
            "rects": rects,
            "hlines": hlines,
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
    if not contents:
        raise HTTPException(status_code=400, detail="הקובץ ריק.")

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            records = []
            for page in pdf.pages:
                page_records = parse_class_schedule(page)
                if page_records:
                    records.extend(page_records)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"קריאת ה-PDF נכשלה: {exc}")

    for rec in records:
        rec.pop("_bbox", None)

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהתה מערכת שעות בקובץ. ודא שהקובץ הוא מערכת שעות שבועית במבנה יומן (ימים בעמודות, שעות בשורות).",
        )

    return records
