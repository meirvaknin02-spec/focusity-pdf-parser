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
import struct
from collections import Counter
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
from pdfminer.pdffont import FileUnicodeMap, PDFCIDFont, TrueTypeFont

# Previously there was no logging at all: a parse failure's real cause (a
# corrupt PDF, an unexpected pdfminer exception, ...) was visible ONLY by
# echoing the raw exception text back to the client -- which also meant an
# internal detail (a Python exception message, occasionally including a
# library-internal path or type name) was exposed in the HTTP response
# instead of staying server-side. Uvicorn's default logging config already
# ships everything through to stdout/stderr, so this needs no extra setup
# to show up wherever the service's logs are viewed (locally or on Render).
logger = logging.getLogger("focusity_pdf_parser")


# A font's /ToUnicode CMap is what turns the glyph numbers in a PDF back into
# letters. Some Hebrew exports ship one that only covers digits, punctuation
# and Latin: Levinsky-Wingate's registration-system timetable (Word, the David
# font, Type0 / Identity-H) maps glyphs 0x11-0x47 and nothing else, so every
# Hebrew letter came out of pdfplumber as "(cid:170)", no header keyword could
# match, and a perfectly ruled 26-class timetable was rejected as "no table".
# The letters are not lost: the embedded TrueType font still carries its own
# Unicode cmap, and with Identity-H a CID is the glyph id. pdfminer only reads
# that cmap when /ToUnicode is missing altogether, so a partial one hides it.
#
# Glyphs the /ToUnicode leaves out are filled from the embedded cmap; a glyph
# it does name is never second-guessed. Where the cmap gives one glyph two
# characters, the real one beats a private-use code point: David maps its nun
# to both U+05E0 and U+F8FF, and taking the last one dropped every נ
# ("התנועה" became "התועה").
_PRIVATE_USE = range(0xE000, 0xF900)


def _embedded_cid_to_unicode(fontfile) -> dict:
    ttf = TrueTypeFont("embedded", io.BytesIO(fontfile.get_data()))
    if b"cmap" not in ttf.tables:
        return {}
    base, _length = ttf.tables[b"cmap"]
    fp = ttf.fp
    fp.seek(base)
    _version, count = struct.unpack(">HH", fp.read(4))
    subtables = [struct.unpack(">HHL", fp.read(8)) for _ in range(count)]
    char2gid: dict = {}
    for platform, encoding, offset in subtables:
        # Unicode subtables only; a Mac Roman one would map bytes, not letters.
        if not (platform == 0 or (platform == 3 and encoding in (1, 10))):
            continue
        fp.seek(base + offset)
        (fmt,) = struct.unpack(">H", fp.read(2))
        reader = getattr(ttf, f"parse_cmap_format_{fmt}", None)
        if reader:
            per_table: dict = {}
            reader(fp, per_table)
            char2gid.update(per_table)
    gid2char: dict = {}
    for char, gid in sorted(char2gid.items()):
        current = gid2char.get(gid)
        if current is None or (current in _PRIVATE_USE and char not in _PRIVATE_USE):
            gid2char[gid] = char
    return {gid: chr(char) for gid, char in gid2char.items() if char >= 0x20}


_original_cid_font_init = PDFCIDFont.__init__


def _cid_font_init_with_embedded_letters(self, rsrcmgr, spec, *args, **kwargs):
    _original_cid_font_init(self, rsrcmgr, spec, *args, **kwargs)
    fontfile = getattr(self, "fontfile", None)
    if (
        fontfile is None
        or self.cidcoding != "Adobe-Identity"
        or not isinstance(self.unicode_map, FileUnicodeMap)
        # Only when CID == glyph id; an explicit CIDToGIDMap stream breaks that.
        or not str(spec.get("CIDToGIDMap", "/'Identity'")).endswith("Identity'")
    ):
        return
    try:
        embedded = _embedded_cid_to_unicode(fontfile)
    except Exception:  # a broken embedded font must never fail the whole PDF
        return
    known = self.unicode_map.cid2unichr
    for cid, char in embedded.items():
        known.setdefault(cid, char)


PDFCIDFont.__init__ = _cid_font_init_with_embedded_letters

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
# A number keeps its inner separators: "1.5", "12:00" and "27/12/2026" are
# one run each, so reversing the run ORDER can never turn them into "5.1",
# "00:12" or "2026/12/27" (all three were seen on real SCE/Sapir exports).
# A Latin word or dotted abbreviation is kept whole the same way:
# Levinsky-Wingate's room "M.P.E / מעבדה 616" came back as "E.P.M" when it
# was reversed along with the Hebrew around it.
RUN_RE = re.compile(r"\d+(?:[.,:/]\d+)*|[A-Za-z]+(?:\.[A-Za-z]+)*|[^\dA-Za-z]+")
KEEP_RUN_RE = re.compile(r"[\dA-Za-z]")
# Brackets are mirror glyphs: a reversed "(קמפוס)" must come back as
# "(קמפוס)", not ")קמפוס(" -- the shape that leaked into course names.
MIRROR_BRACKETS = str.maketrans("()[]{}<>", ")(][}{><")

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
    return "".join(run if KEEP_RUN_RE.match(run) else run[::-1].translate(MIRROR_BRACKETS) for run in runs)


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


# "9:00 AM", "2:00PM" -- the 12-hour clock English exam timetables print.
EXAM_AMPM_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?\b")
# "ACCTG 102*", "ACADENG 100", "COMPSCI 399" at the start of an exam line:
# outside Israel the course code opens the row and everything after the time
# is logistics (campus, permitted materials, invigilation mode).
LATIN_CODE_PREFIX_RE = re.compile(r"^([A-Z]{2,8})\s?(\d{3,5}[A-Z]?)\*?(?=\s|$)")


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
        # The 12-hour clock needs its own pass, and not only to add 12 hours.
        # These rows also carry a DURATION column ("02:30"), and since the
        # bare-time branch below sorts a set, that duration sorted ahead of
        # the real start time and became it -- every row of a real Auckland
        # timetable came out starting at its own length. With AM/PM present,
        # only the labelled times are clock times; a leftover bare time short
        # enough to be a duration is used as one.
        ampm = EXAM_AMPM_TIME_RE.findall(line)
        if ampm:
            times = []
            for h, m, half in ampm:
                h, m = int(h), int(m)
                if 1 <= h <= 12 and m <= 59:
                    times.append(f"{h % 12 + (12 if half.lower() == 'p' else 0):02d}:{m:02d}")
            if len(times) == 1:
                bare = [(int(h), int(m)) for h, m in TIME_RE.findall(line)]
                labelled = {(int(h) % 12 + (12 if half.lower() == "p" else 0), int(m))
                            for h, m, half in ampm}
                # A 12-hour reading appears in `bare` as itself ("2:00"
                # for 2:00 PM), so compare on the clock face before deciding
                # a leftover is a duration.
                durations = [(h, m) for h, m in bare
                             if h <= 6 and not any(m == lm and h % 12 == lh % 12
                                                   for lh, lm in labelled)]
                if len(durations) == 1:
                    sh, sm = int(times[0][:2]), int(times[0][3:])
                    end = sh * 60 + sm + durations[0][0] * 60 + durations[0][1]
                    if end < 24 * 60:
                        times.append(f"{end // 60:02d}:{end % 60:02d}")
        else:
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

        # A line that opens with a course code names the course and nothing
        # else does: the rest of an English exam row is campus, permitted
        # materials and invigilation mode, all of which ended up inside the
        # course name and made every exam import unreadable.
        latin_code = LATIN_CODE_PREFIX_RE.match(line)
        if latin_code:
            code = f"{latin_code.group(1)} {latin_code.group(2)}"
            record = {
                "course_name": code,
                "date": date_iso,
                "start_time": times[0],
                "end_time": times[1] if len(times) > 1 else None,
                "course_code": code,
            }
            records.append(record)
            continue

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


# "קלקולוס 1 - שיעור (קמפוס)", "מבוא לכלכלה -תרגול (לסרוגין:קמפוס ומקוון)",
# "סטטיסטיקה ישומית-ת": the institution packs the meeting type and the
# delivery mode into the course name. Left there, the lecture and the
# practice session of one course become two courses in the app (confirmed
# on a real Sapir timetable: 6 courses came out as 10). The name keeps
# only the course; the rest travels in its own fields.
MEETING_TYPE_TOKENS = [
    # (regex on the trailing part of the name, canonical type)
    (r"(?:^|[\s\-–—])(?:תרגול|תירגול|תרגיל)(?:\s+(?:במכללה|בקמפוס|מקוון))?\s*$", "practice"),
    (r"(?:^|[\s\-–—])(?:שיעור|הרצאה)\s*$", "lecture"),
    (r"(?:^|[\s\-–—])(?:מעבדה|מעבדת)\s*$", "lab"),
    (r"(?:^|[\s\-–—])(?:סדנה|סמינר)\s*$", "workshop"),
    (r"\s*-\s*ת\s*$", "practice"),   # SCE's "-ת"
    (r"\s*-\s*ה\s*$", "lecture"),    # SCE's "-ה"
    (r"\s*-\s*מ\s*$", "lab"),        # SCE's "-מ"
    (r"(?:^|\s)(?:practice|tutorial|recitation|exercise)\s*$", "practice"),
    (r"(?:^|\s)(?:lecture)\s*$", "lecture"),
    (r"(?:^|\s)(?:lab|laboratory)\s*$", "lab"),
]
PARENTHETICAL_RE = re.compile(r"\s*[(\[][^()\[\]]{0,60}[)\]]\s*")


def split_course_name(raw: str):
    """-> (course_name, meeting_type, notes). The parenthetical delivery
    mode ("(קמפוס)", "(לסרוגין:קמפוס ומקוון)") becomes `notes`; a trailing
    meeting-type word becomes `meeting_type`; dangling " - " is trimmed."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    notes = " ".join(m.strip(" ()[]") for m in PARENTHETICAL_RE.findall(name)).strip()
    name = PARENTHETICAL_RE.sub(" ", name).strip()
    meeting_type = ""
    for pattern, kind in MEETING_TYPE_TOKENS:
        m = re.search(pattern, name, flags=re.IGNORECASE)
        if m:
            meeting_type = kind
            name = name[:m.start()].rstrip()
            break
    name = re.sub(r"[\s\-–—]+$", "", name).strip()
    return name or str(raw or "").strip(), meeting_type, notes


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


# --- Transposed grid: hours across the top, days down the side ---------------
# Everything from here to parse_class_schedule serves the layout the Israeli
# exports do NOT use. It was added after four real English-language timetables
# (Trinidad, India x2, New Zealand) all returned zero rows: none of them was a
# language problem -- the day names and header keywords were already matched --
# they simply draw the same week the other way round.

TIME_AMPM_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?$", re.IGNORECASE)
RANGE_DASH_WORDS = {"-", "–", "—", "to"}
LABELLED_LINE_RE = re.compile(
    r"^(course|module|subject|staff|lecturer|teacher|room|venue|location)\s*:\s*(.*)$",
    re.IGNORECASE,
)
ACTIVITY_RE = re.compile(
    r"^(lecture|tutorial|laboratory|lab|practical|workshop|seminar|postgraduate)\b",
    re.IGNORECASE,
)
ACTIVITY_TO_TYPE = {
    "lecture": "lecture", "tutorial": "practice", "practical": "practice",
    "laboratory": "lab", "lab": "lab", "workshop": "workshop", "seminar": "workshop",
    "postgraduate": "",
}
# "MATH 1142", "SOWK6000", "CS 101A" -- what a course code looks like outside
# Israel, where COURSE_CODE_RE's "01234567-1" shape never appears.
LATIN_COURSE_CODE_RE = re.compile(r"^([A-Z]{2,6})\s?(\d{3,5}[A-Z]?)$")


def _time_label_minutes(text: str) -> Optional[int]:
    """'09:30' -> 570, '08:00AM' -> 480, '01:05 PM' -> 785; None if the word
    is not a clock time. The 24-hour form is what the Israeli exports print
    and the 12-hour one what the English ones do -- a header reading
    "08:00AM" matched nothing at all before this, so every CELCAT timetable
    lost its entire hour ladder and was rejected."""
    t = (text or "").strip()
    m = TIME_LABEL_RE.match(t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        return hh * 60 + mm if hh <= 23 and mm <= 59 else None
    m = TIME_AMPM_RE.match(t)
    if m:
        hh, mm, half = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if not (1 <= hh <= 12 and mm <= 59):
            return None
        return (hh % 12 + (12 if half == "p" else 0)) * 60 + mm
    return None


def _hour_columns(words):
    """The hour header of a transposed grid -> ([(x_center, start_min,
    end_min)], the y the header ends at).

    Two conventions appear in real exports and they mean different things. A
    column headed by ONE time ("08:00AM") names the moment the column starts
    and runs until the next column's time. A column headed by a RANGE
    ("8:30 - 9:25") carries its own end, and reading that pair as two
    separate columns would halve every class on the page. The dash printed
    between the two halves is what distinguishes them -- it is the only
    signal in the geometry, since the gap inside a range is not reliably
    smaller than the gap between neighbouring columns."""
    marks = []
    for w in words:
        minutes = _time_label_minutes(w["text"])
        if minutes is not None:
            marks.append({"x0": w["x0"], "x1": w["x1"], "top": w["top"],
                          "bottom": w["bottom"], "min": minutes})
    if len(marks) < 2:
        return [], 0.0

    # The header is the horizontal band holding the most time labels, so a
    # footer print-time or a time typed inside a class cell cannot join it.
    band = max(
        ([m for m in marks if abs(m["top"] - anchor["top"]) <= 4] for anchor in marks),
        key=len,
    )
    if len(band) < 2:
        return [], 0.0
    band.sort(key=lambda m: m["x0"])
    header_top = band[0]["top"]
    header_bottom = max(m["bottom"] for m in band)
    dashes = [
        (w["x0"] + w["x1"]) / 2
        for w in words
        if w["text"].strip().lower() in RANGE_DASH_WORDS and abs(w["top"] - header_top) <= 4
    ]

    columns = []
    i = 0
    while i < len(band):
        cur = band[i]
        nxt = band[i + 1] if i + 1 < len(band) else None
        if nxt is not None and any(cur["x1"] <= d <= nxt["x0"] for d in dashes):
            columns.append([(cur["x0"] + nxt["x1"]) / 2, cur["min"], nxt["min"]])
            i += 2
        else:
            columns.append([(cur["x0"] + cur["x1"]) / 2, cur["min"], None])
            i += 1

    # A single-time column ends where the next one starts; the last one
    # borrows the typical width of the ones before it.
    for idx, col in enumerate(columns):
        if col[2] is None:
            if idx + 1 < len(columns):
                col[2] = columns[idx + 1][1]
            else:
                widths = [c[2] - c[1] for c in columns[:idx] if c[2] is not None]
                col[2] = col[1] + int(_median(widths) if widths else 60)
    return [tuple(c) for c in columns if c[2] > c[1]], header_bottom


LABEL_TO_FIELD = {
    "course": "course_name", "module": "course_name", "subject": "course_name",
    "staff": "lecturer", "lecturer": "lecturer", "teacher": "lecturer",
    "room": "room", "venue": "room", "location": "room",
}
# How many wrapped lines a value may run to after its label. A course title
# can be several lines ("ELFU / Writing / Workshop"); a room or a lecturer
# cannot, and letting them run on swallowed the italic repeat of the course
# title that CELCAT prints at the foot of the box.
LABEL_CONTINUATION_LIMIT = {"course_name": 4, "lecturer": 3, "room": 2}


def _read_cell_fields(lines: list) -> dict:
    """A class cell's lines -> the record's text fields.

    CELCAT and the exports that copy it label their lines ("Course: MATH
    1142", "Staff: TYAGI, Shikhar", "Room: TLC TR6"), which is far more
    reliable than guessing by shape the way the Hebrew path has to. The boxes
    are narrow, so a label and its value are usually on separate lines and a
    value wraps over several -- the label opens a field that stays open until
    the next label or its continuation limit. When no line is labelled at
    all, the cell falls back to the same guesswork as the Hebrew path."""
    out = {"course_name": "", "meeting_type": "", "notes": "",
           "lecturer": "", "room": "", "course_code": ""}
    buckets = {"course_name": [], "lecturer": [], "room": []}
    leftovers = []
    current = None

    for text in lines:
        m = LABELLED_LINE_RE.match(text)
        if m:
            current = LABEL_TO_FIELD[m.group(1).lower()]
            value = m.group(2).strip()
            if value:
                buckets[current].append(value)
            continue
        m = ACTIVITY_RE.match(text)
        if m and not out["meeting_type"] and current is None:
            out["meeting_type"] = ACTIVITY_TO_TYPE.get(m.group(1).lower(), "")
            continue
        if current and len(buckets[current]) < LABEL_CONTINUATION_LIMIT[current]:
            buckets[current].append(text)
        else:
            leftovers.append(text)

    for field, parts in buckets.items():
        out[field] = " ".join(parts).strip()

    if out["course_name"]:
        out["notes"] = " ".join(leftovers)[:200]
    else:
        name, meeting_type, notes = split_course_name(" ".join(leftovers))
        out["course_name"] = name
        out["meeting_type"] = out["meeting_type"] or meeting_type
        out["notes"] = notes

    code = COURSE_CODE_RE.search(out["course_name"])
    if code:
        out["course_code"] = code.group(1)
    elif LATIN_COURSE_CODE_RE.match(out["course_name"].strip()):
        # The cell gives a code and no title: keep it in both fields rather
        # than inventing a course name the document does not contain.
        out["course_code"] = out["course_name"].strip()
    return out


def _looks_like_merged_rows(lines: list) -> bool:
    """True when a box holds several stacked sub-rows rather than one class.

    A department-wide timetable prints one row per programme inside the same
    day band (IIT Bhubaneswar draws eight: CE, CSE, ECE, EE, EP, MC, ME,
    SMMME), and the drawn box covers all of them -- so the cell comes out
    holding the same course eight times over. Importing that gives a student
    a week of garbage, and no amount of geometry can tell which of the eight
    rows is theirs; the document simply does not say. Failing the page is the
    honest outcome: the endpoint then reports that no timetable was
    recognized instead of writing nonsense into someone's calendar.

    One real class never repeats a word of its own four times."""
    words = [w for line in lines for w in line.split()]
    if len(words) < 8:
        return False
    return Counter(words).most_common(1)[0][1] >= 4


def _parse_transposed_grid(page, words, day_marks):
    """Hours across the top, days down the side. Returns records in exactly
    the shape parse_class_schedule returns, or None when the page turns out
    not to be a grid after all. Called only from parse_class_schedule."""
    columns, header_bottom = _hour_columns(words)
    if len(columns) < 2:
        return None

    day_rows = sorted(((y, d) for _, y, d in day_marks))
    row_gaps = [day_rows[i + 1][0] - day_rows[i][0] for i in range(len(day_rows) - 1)]
    row_h = _median(row_gaps) if row_gaps else 40
    col_w = _median([columns[i + 1][0] - columns[i][0] for i in range(len(columns) - 1)]) or 40
    # Left of the first hour column is the day-label strip, not class cells.
    grid_left = columns[0][0] - col_w
    # The hour header's own cells are boxes too: without this line every
    # column heading came back as a class called "08:00AM".
    grid_top = header_bottom

    def nearest_day(yc):
        return min(day_rows, key=lambda p: abs(p[0] - yc))[1]

    # A class box is not always a `rect`. CELCAT draws its boxes with rounded
    # corners, which pdfplumber reports as curves, and prints a grey header
    # strip inside each box as one rect PER WRAPPED LINE -- reading rects
    # alone turned every class into three records named after fragments of
    # its own header ("Wks W5-", "W12 [=8]"). So both shapes are candidates,
    # and a box that sits inside a bigger one is decoration, not a class.
    #
    # The size bounds drop the table's own border and any full-week banner:
    # a real class never spans the whole week, and a box taller than a day
    # row is that row's frame rather than one class.
    boxes = [
        b for b in (list(page.rects) + list(page.curves))
        if b["height"] >= 6
        and col_w * 0.4 <= b["width"] <= col_w * (len(columns) + 1)
        and b["height"] <= row_h * 1.6
        and (b["x0"] + b["x1"]) / 2 >= grid_left
        and b["top"] >= grid_top - 2
    ]
    boxes.sort(key=lambda b: b["width"] * b["height"], reverse=True)
    cells = []
    for b in boxes:
        if any(
            o["x0"] - 1 <= b["x0"] and b["x1"] <= o["x1"] + 1
            and o["top"] - 1 <= b["top"] and b["bottom"] <= o["bottom"] + 1
            for o in cells
        ):
            continue
        cells.append(b)

    seen = set()
    records = []
    for r in cells:
        covered = [c for c in columns if r["x0"] - col_w * 0.25 <= c[0] <= r["x1"] + col_w * 0.25]
        if not covered:
            continue
        lines = _cell_lines(page, r)
        if not lines or _looks_like_merged_rows(lines):
            continue

        start, end = covered[0][1], max(c[2] for c in covered)
        if end <= start:
            continue
        fields = _read_cell_fields(lines)
        if not fields["course_name"]:
            continue

        day = nearest_day((r["top"] + r["bottom"]) / 2)
        start_time = f"{start // 60:02d}:{start % 60:02d}"
        # One class is often drawn as two nested rectangles (a coloured frame
        # inside a plain one), which would otherwise be imported twice.
        key = (day, start_time, fields["course_name"])
        if key in seen:
            continue
        seen.add(key)

        records.append({
            "course_name": fields["course_name"],
            "meeting_type": fields["meeting_type"],
            "notes": fields["notes"],
            "day": day,
            "start_time": start_time,
            "end_time": f"{end // 60:02d}:{end % 60:02d}",
            "lecturer": fields["lecturer"],
            "room": fields["room"],
            "course_code": fields["course_code"],
            "credits": "",
        })

    if not records or _too_many_clashes(records):
        return None
    records.sort(key=lambda c: (c["day"], c["start_time"]))
    return records


def _too_many_clashes(records: list) -> bool:
    """True when the week this produced cannot be one person's.

    Nobody attends two classes at once, so a handful of clashes means a real
    timetable with a real clash in it, while a page full of them means the
    layout was misread -- most often a department-wide document that stacks
    one row per programme inside each day band, where the drawn boxes cover
    several programmes at a time. Returning nothing then is deliberate: the
    endpoint reports that no timetable was recognized, which is recoverable,
    instead of filling a student's week with other programmes' classes,
    which looks like a working import and is not."""
    clashing = set()
    for i, a in enumerate(records):
        for b in records[i + 1:]:
            if a["day"] != b["day"]:
                continue
            if a["start_time"] < b["end_time"] and b["start_time"] < a["end_time"]:
                clashing.add(id(a))
                clashing.add(id(b))
    return len(clashing) > len(records) * 0.25


def parse_class_schedule(page):
    """Parse a weekly-calendar-grid timetable (days across columns, half-hour
    rows, each class a bordered cell). Returns records or None if the page is
    not a recognizable grid schedule.

    Geometry is derived entirely from the PDF's own day-header words, time
    labels, and class-cell rectangles -- no hardcoded coordinates -- so it
    generalizes across institutions that export this calendar layout."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)

    # 1. Day labels. A day name is not always printed upright in a header
    # row: plenty of exports write it sideways down the left edge instead.
    # pdfplumber returns a rotated word with its characters in page order,
    # which reads backwards ("yadnoM"), so a non-upright word is also tried
    # reversed -- without that, three real English timetables produced zero
    # day matches and the whole page was rejected as "not a schedule".
    day_marks = []  # (x_center, y_center, day_idx)
    header_bottom = 0
    for w in words:
        t = fix_bidi_line(w["text"]).strip()
        day_idx = _day_index(t)
        if day_idx is None and not w.get("upright", True):
            day_idx = _day_index(t[::-1])
        if day_idx is not None:
            day_marks.append(((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, day_idx))
            header_bottom = max(header_bottom, w["bottom"])
    if len(day_marks) < 3:
        return None

    # 2. Which way round is this grid? Days spread along x is the layout the
    # rest of this function reads (days across columns, hours down the rows).
    # Days spread along y is its transpose -- hours across the top, days down
    # the side -- which is what CELCAT (the timetabling system behind most
    # UK/Commonwealth university exports) and most Indian institutes print.
    # The two need different geometry at every step, so the transpose gets
    # its own function rather than an axis flag threaded through this one;
    # that also means this path behaves exactly as it did before.
    x_spread = max(m[0] for m in day_marks) - min(m[0] for m in day_marks)
    y_spread = max(m[1] for m in day_marks) - min(m[1] for m in day_marks)
    if y_spread > x_spread:
        return _parse_transposed_grid(page, words, day_marks)

    day_centers = [(x, d) for x, _, d in day_marks]

    def nearest_day(xc):
        return min(day_centers, key=lambda p: abs(p[0] - xc))[1]

    # Day-column width from the spacing between adjacent day centers.
    xs = sorted(c for c, _ in day_centers)
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    col_w = _median(gaps) if gaps else 80

    # 3. Time labels (below the header) -> discrete y->time snap points.
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

    # The hour column only names whole rows (30 minutes on every export seen so
    # far), but not every institution draws its class blocks on those lines:
    # Sapir also puts them on HALF rows, i.e. quarter past and quarter to.
    # Snapping such a block to the nearest *printed* label pulled it back to the
    # label above - on the real Sapir timetable three of ten classes came out a
    # quarter of an hour early, as valid-looking JSON with no warning. So the
    # ladder also carries the midpoint between consecutive labels.
    #
    # Nearest-match is kept on purpose: a block's edge sits a couple of points
    # off the label it belongs to, and matching by distance absorbs that offset
    # where interpolating from label coordinates would not.
    snap_points = []  # (y, minutes)
    for (y_a, label_a), (y_b, label_b) in zip(time_labels, time_labels[1:]):
        m_a, m_b = _label_to_minutes(label_a), _label_to_minutes(label_b)
        snap_points.append((y_a, m_a))
        if m_b > m_a:
            snap_points.append(((y_a + y_b) / 2.0, (m_a + m_b) // 2))
    snap_points.append((time_labels[-1][0], _label_to_minutes(time_labels[-1][1])))

    def snap_time(y):
        minutes = min(snap_points, key=lambda p: abs(p[0] - y))[1]
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # 4. Class cells: rectangles below the header, one day-column wide, sitting
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

    # 5. For each class cell, read its own lines and classify them.
    #
    # The credits/weekly-hours lines ("נ"ז3:", "ש"ש1.5:") are drawn UNDER the
    # class rectangle on Sapir's export -- the box covers the class time, the
    # metadata overflows below it -- so a strict in-rect crop lost them for
    # every class but the tallest (2 of 4 credit values on a real file).
    # Each cell also reads a thin band below itself, capped at the next class
    # box in the same column, and keeps only metadata-shaped lines from it.
    def band_below(rect, day_idx):
        floor = rect["bottom"] + max(row_gap * 1.5, 14)
        for other, other_day in class_rects:
            if other is rect or other_day != day_idx:
                continue
            if other["top"] >= rect["bottom"] - 1 and other["top"] < floor:
                floor = other["top"]
        if floor <= rect["bottom"] + 1:
            return []
        band = {"top": rect["bottom"], "bottom": floor, "x0": rect["x0"], "x1": rect["x1"]}
        return [ln for ln in _cell_lines(page, band) if METADATA_RE.search(ln) or COURSE_CODE_RE.search(ln)]

    records = []
    for r, day in class_rects:
        lines = _cell_lines(page, r)
        if not lines:
            continue
        lines = lines + band_below(r, day)

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
                # "נ"ז3:" as printed, or ":3ז"נ" when a generator flipped it.
                cm = re.search(r'נ["״]ז[:\s]*([0-9]+(?:\.[0-9]+)?)', text) or re.search(r'([0-9]+(?:\.[0-9]+)?)\s*:?\s*ז["״]נ', text)
                if cm and credits is None:
                    credits = float(cm.group(1))
                    if credits.is_integer():
                        credits = int(credits)
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

        course_name, meeting_type, notes = split_course_name(" ".join(name_lines))
        location = " ".join(x for x in (building, room_num) if x).strip()

        records.append({
            "course_name": course_name,
            "meeting_type": meeting_type,
            "notes": notes,
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
    # Bare "שעור" (no yod) is how SCE's Michlol weekly print view heads it.
    "course_name": ["שם שיעור", "שם השיעור", "שם שעור", "שם הקורס", "שם קורס", "שם מקצוע", "מקצוע", "שיעור", "שעור", "קורס",
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


# Some colleges print the whole year in one document: Levinsky-Wingate's
# registration-system timetable is a "סמסטר א'" table and a "סמסטר ב'" table,
# each under a full-width banner row. Read as one week, a course that runs in
# both semesters arrived twice and semester B's classes overlapped semester
# A's on the same days. Each record is tagged with the banner above it, and
# the endpoint keeps only the semester that is running now.
SEMESTER_RE = re.compile(r"סמסטר\s*(א|ב|קיץ)(?![א-ת])")
SEMESTER_KEYS = {"א": "A", "ב": "B", "קיץ": "summer"}


def _semester_banner(row) -> Optional[str]:
    """A row with one filled cell naming a semester. Tried in both reading
    directions: the banner sits above the header that decides the table's."""
    if not row:
        return None
    filled = [c for c in row if normalize_cell(c)]
    if len(filled) != 1:
        return None
    for reverse in (False, True):
        match = SEMESTER_RE.search(normalize_cell(filled[0], reverse))
        if match:
            return SEMESTER_KEYS[match.group(1)]
    return None


def _today() -> date:
    return date.today()


def _running_semester(today: date, present: set) -> str:
    """Israeli academic calendar: semester A runs October to February,
    semester B March to June. July-August is the summer semester when the
    document has one; otherwise, and in September, the next thing a student
    is preparing for is semester A."""
    if today.month in (10, 11, 12, 1, 2):
        return "A"
    if today.month in (3, 4, 5, 6):
        return "B"
    if today.month in (7, 8) and "summer" in present:
        return "summer"
    return "A"


def keep_running_semester(records: list) -> list:
    """Drops the other semesters' classes from a whole-year document. A
    record before the first banner keeps no tag and is never dropped; a
    document with one semester, or without the running one, is returned
    whole -- importing too much is fixable in the review table, an empty
    import is not."""
    tagged, last = [], None
    for record in records:
        semester = record.pop("_semester", None) or last
        last = semester
        tagged.append((record, semester))
    present = {s for _, s in tagged if s}
    if len(present) < 2:
        return [r for r, _ in tagged]
    want = _running_semester(_today(), present)
    if want not in present:
        return [r for r, _ in tagged]
    return [r for r, s in tagged if s in (want, None)]


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

    header = table[header_idx]
    mapped_columns = set(mapping.values())

    def cell(row, field):
        idx = mapping.get(field)
        if idx is None or idx >= len(row):
            return ""
        # A value cell drawn wider than its header cell: pdfplumber puts the
        # text in the cell's leftmost column and None in the columns it
        # covers. SCE's Michlol weekly print view heads the course name
        # "שעור" over the right half of a two-column cell whose left half
        # has a blank header, so every name sat one column to the left of
        # its header and the whole table read as nameless rows. Borrowed
        # only from a blank-headed column no other field owns.
        if row[idx] is None:
            left = idx - 1
            while left >= 0 and row[left] is None:
                left -= 1
            if (left >= 0 and left not in mapped_columns
                    and not normalize_cell(header[left] if left < len(header) else None)):
                return normalize_cell(row[left], reverse)
        return normalize_cell(row[idx], reverse)

    semester = None
    for row in table[:header_idx]:
        semester = _semester_banner(row) or semester

    records = []
    last_day = None
    for row in table[header_idx + 1:]:
        if row is None or all(not normalize_cell(c) for c in row):
            continue
        banner = _semester_banner(row)
        if banner:
            semester, last_day = banner, None
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

        course_name, meeting_type, notes = split_course_name(course_name)
        records.append({
            "course_name": course_name,
            "meeting_type": meeting_type,
            "notes": notes,
            "day": day,
            "start_time": start_time,
            "end_time": end_time,
            "lecturer": cell(row, "lecturer"),
            "room": cell(row, "room"),
            "course_code": cell(row, "course_code"),
            "credits": credits if credits is not None else "",
            "_semester": semester,
        })

    records.sort(key=lambda c: (c["day"], c["start_time"] or ""))
    return records


# Generous enough for a real multi-page schedule/transcript, but bounded so
# one adversarial or pathologically slow PDF can't hang a request forever.
#
# 30 was not generous enough. A real published exam timetable -- the
# University of Auckland's, 9 pages and 507 exams, the file a student there
# would actually download -- takes about 20 seconds on a developer laptop and
# more than 30 on Render's free instance, so it returned 504 while parsing
# perfectly in the test suite. Most of that time is the two table strategies
# that run over every page before the text-line one succeeds.
#
# 55 is bounded by what the caller already waits: the web app gives the
# parser 75 seconds (PARSER_TIMEOUT_MS in pdfParserClient.js) before it
# aborts, so this stays below that and the student still gets a real error
# rather than a hung dialog. Raising it does not make anyone wait longer than
# they already would -- it only lets a slow but legitimate document finish.
PARSE_TIMEOUT_SECONDS = 55

# How many pages a strategy may read before its emptiness is taken as proof
# that it is the wrong strategy for this document (see _extract_records_sync).
# Two, because every real export seen so far that needs a given strategy
# yields its first row on page one, and the two-page fixtures here are then
# covered end to end by the probe itself.
PROBE_PAGES = 2

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
            for index, page in enumerate(pdf.pages):
                records.extend(page_records(page))
                # A strategy that has found nothing in the first few pages of
                # a long document is not going to be the one that reads it,
                # and table detection is expensive: on the University of
                # Auckland's 9-page exam timetable the two table strategies
                # cost 17 of the 20 seconds and returned nothing, which was
                # enough to blow the request budget on Render's free CPU and
                # turn a file that parses perfectly into a timeout.
                #
                # The cost of the shortcut is a document whose table starts
                # only after a long preamble: that one now falls through to
                # the next strategy instead. Documents of PROBE_PAGES pages
                # or fewer -- which is nearly all of them, and every fixture
                # here -- are unaffected, since the probe covers the whole
                # file.
                if not records and index + 1 >= PROBE_PAGES:
                    break
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
    records = keep_running_semester(records)

    if not records:
        raise HTTPException(
            status_code=422,
            detail="לא זוהתה מערכת שעות בקובץ. ודא שהקובץ הוא מערכת שעות שבועית במבנה יומן (ימים בעמודות, שעות בשורות).",
        )

    return records
