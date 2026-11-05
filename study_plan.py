"""Day-by-day study plans: a table whose rows are DATES (not weekdays) and
whose cells say what to study that day.

Why this exists: on 2026-11-05 a student who had just signed up made a plan
like this (rows "Nov 6 (Fri)" .. "Dec 27", one column per subject, a topic in
each cell, no hours), uploaded it three times as a timetable and left with
nothing. It was the third such upload in three weeks. A weekly timetable
parser can never read it -- nothing repeats weekly -- so it gets its own
reader and its own endpoint, and the web app turns each cell into one study
session on its date.

Two shapes are read:
  * wide  -- a date column, then one column per subject; the header cell is
             the subject and each cell is that day's topic (the real upload);
  * long  -- a date column plus a "subject"/"course" column and a
             "topic"/"task" column, one row per session.
An optional time column (or a time range inside the date cell) gives the
session its hours; without one the student picks the hour in the app.

What is refused on purpose: anything whose header names a room, a lecturer,
an exam sitting or a duration. That is an exam list or a class list sent to
the wrong importer, and calling it a study plan would put the exams into the
calendar as homework.
"""
import re
from datetime import date, timedelta
from typing import Callable, Optional

# Kept free of main.py so it can be tested on bare tables; main passes in its
# cell normaliser (bidi fix) and its clock (pinned by the tests).

MONTHS_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTHS_HE = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
    "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}

ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
# 6.11 / 06/11/2026 / 6.11.26. Not part of a clock ("10:30") or a longer number.
NUMERIC_RE = re.compile(r"(?<![\d:.,/])(\d{1,2})[./](\d{1,2})(?:[./](\d{2}|\d{4}))?(?![\d:])")
MONTH_DAY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(20\d{2}))?")
DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\b\.?(?:,?\s+(20\d{2}))?")
HE_DAY_MONTH_RE = re.compile(r"(\d{1,2})\s+[בל]?(" + "|".join(MONTHS_HE) + r")(?:\s+(20\d{2}))?")

WEEKDAY_RE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b\.?"
    r"|יום\s*[אבגדהוש]['׳]?|(?<![א-ת])(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)(?![א-ת])"
    r"|(?<![א-ת])[אבגדהוש]['׳](?![א-ת])",
    re.IGNORECASE,
)
# A date cell may say "Day 3" or "Week 2" beside the date; nothing else.
DATE_CELL_EXTRA_RE = re.compile(r"\b(?:day|week)\s*\d+\b|יום\s*\d+|שבוע\s*\d+", re.IGNORECASE)

TIME_RANGE_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*(?:[-–—]|to|עד)\s*(\d{1,2})[:.](\d{2})")
TIME_ONE_RE = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?![\d])")

# A cell that means "nothing today". "DIWALI HOLIDAY" is a day off, but
# "Rest of chapter 3" and "Break-even analysis" are topics: the strong words
# count in a short cell, the weak ones only when they are the whole cell.
DAY_OFF_STRONG_RE = re.compile(
    r"\b(?:holidays?|vacation|day\s*off|free\s*day|no\s*study)\b|חופש|(?<![א-ת])חג(?![א-ת])|אין\s*לימוד|יום\s*חופשי",
    re.IGNORECASE,
)
DAY_OFF_WEAK_RE = re.compile(r"^(?:rest|off|break|free|rest\s*day|מנוחה|הפסקה|[-–—xX/.]+|n/?a|none)$", re.IGNORECASE)


def is_day_off(text: str) -> bool:
    text = _squash(text).strip(" .!*()")
    if not text:
        return True
    if DAY_OFF_WEAK_RE.match(text):
        return True
    return bool(DAY_OFF_STRONG_RE.search(text)) and len(text.split()) <= 4

SUBJECT_HEADER_RE = re.compile(r"^(?:subjects?|course|courses|class|module|מקצוע|מקצועות|קורס|קורסים|שיעור)$", re.IGNORECASE)
TOPIC_HEADER_RE = re.compile(
    r"^(?:topics?|tasks?|chapters?|what\s+to\s+study|plan|study\s+plan|content|material|goals?|to\s*do"
    r"|נושא|נושאים|משימה|משימות|פרק|פרקים|חומר|תוכן|מה\s*ללמוד|יעד|יעדים|תוכנית)$",
    re.IGNORECASE,
)
DATE_HEADER_RE = re.compile(r"^(?:date|dates|day|days|weekday|תאריך|תאריכים|יום|ימים)$", re.IGNORECASE)
TIME_HEADER_RE = re.compile(r"^(?:time|times|hours?|slot|שעה|שעות|זמן)$", re.IGNORECASE)
IGNORE_HEADER_RE = re.compile(r"^(?:notes?|remarks?|status|done|check|✓|✔|הערות|הערה|סטטוס|בוצע|סימון|#|no\.?|מס['׳]?)$", re.IGNORECASE)
# An exam list or a class list, not a study plan.
REFUSE_HEADER_RE = re.compile(
    r"\b(?:room|venue|hall|lecturer|instructor|teacher|invigilat\w*|duration|sitting|exams?|tests?|quiz\w*|midterm)\b"
    r"|חדר|כיתה|מרצה|מתרגל|מועד|משך|בחינ|מבחן|בוחן",
    re.IGNORECASE,
)

MIN_DATE_ROWS = 3
MAX_TOPIC_CHARS = 120


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def read_plan_date(cell: str):
    """(day, month, year or None, kind) when the cell IS a date -- a date
    plus at most a weekday, a "Day 3" label and punctuation -- else None.
    kind is 'numeric' when day and month order is still open (6.11 vs 11/6),
    'named' when a month name settled it."""
    text = _squash(cell)
    if not text or len(text) > 40:
        return None
    found = None
    m = ISO_RE.search(text)
    if m:
        found = (int(m.group(3)), int(m.group(2)), int(m.group(1)), "named", m.span())
    if not found:
        m = HE_DAY_MONTH_RE.search(text)
        if m:
            found = (int(m.group(1)), MONTHS_HE[m.group(2)], int(m.group(3)) if m.group(3) else None, "named", m.span())
    if not found:
        for regex, day_g, month_g in ((MONTH_DAY_RE, 2, 1), (DAY_MONTH_RE, 1, 2)):
            m = regex.search(text)
            if m and m.group(month_g)[:3].lower() in MONTHS_EN:
                found = (int(m.group(day_g)), MONTHS_EN[m.group(month_g)[:3].lower()],
                         int(m.group(3)) if m.group(3) else None, "named", m.span())
                break
    if not found:
        m = NUMERIC_RE.search(text)
        if m:
            year = m.group(3)
            year = None if not year else int(year) + (2000 if len(year) == 2 else 0)
            found = (int(m.group(1)), int(m.group(2)), year, "numeric", m.span())
    if not found:
        return None
    day, month, year, kind, (start, end) = found
    rest = text[:start] + " " + text[end:]
    rest = TIME_RANGE_RE.sub(" ", rest)
    rest = WEEKDAY_RE.sub(" ", rest)
    rest = DATE_CELL_EXTRA_RE.sub(" ", rest)
    if len(re.findall(r"[A-Za-z֐-׿]", rest)) > 2:
        return None
    if kind == "named" and not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    if kind == "numeric" and not (1 <= day <= 31 and 1 <= month <= 31):
        return None
    return day, month, year, kind


def _read_times(text: str):
    m = TIME_RANGE_RE.search(text or "")
    if m:
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60 and (h2, m2) > (h1, m1):
            return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"
    m = TIME_ONE_RE.search(text or "")
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}", None
    return None, None


def _is_time_cell(text: str) -> bool:
    return bool(text) and bool(TIME_RANGE_RE.search(text) or TIME_ONE_RE.search(text)) \
        and len(re.findall(r"[A-Za-z֐-׿]", text)) <= 4


def _is_weekday_cell(text: str) -> bool:
    return bool(text) and not WEEKDAY_RE.sub("", text).strip(" .,()-'׳")


class YearGuesser:
    """Gives a year to dates printed without one, in document order.

    A year printed anywhere on the page ("November 6–30, 2026") is used first.
    Otherwise the first date takes the year that puts it nearest after a point
    four months back from today -- a plan is made for the coming weeks, but a
    student may import one already under way. After that the year only moves
    forward: a month that drops by more than three months (Dec -> Jan) is the
    next year."""

    def __init__(self, today: Callable[[], date]):
        self._today = today
        self.year: Optional[int] = None
        self.prev: Optional[date] = None

    def hint(self, year: Optional[int]) -> None:
        if year and self.prev is None:
            self.year = year

    def place(self, day: int, month: int, year: Optional[int]) -> Optional[date]:
        try:
            if year:
                found = date(year, month, day)
            else:
                if self.year is None:
                    floor = self._today() - timedelta(days=120)
                    self.year = floor.year if (month, day) >= (floor.month, floor.day) else floor.year + 1
                found = date(self.year, month, day)
                if self.prev and found < self.prev - timedelta(days=90):
                    found = date(self.year + 1, month, day)
        except ValueError:
            return None
        self.year = found.year
        self.prev = found
        return found


def _date_column(rows):
    """The column holding most of the dates, and how many it holds."""
    width = max((len(r) for r in rows), default=0)
    best, best_hits = -1, 0
    for col in range(width):
        hits = sum(1 for r in rows if col < len(r) and read_plan_date(r[col]))
        if hits > best_hits:
            best, best_hits = col, hits
    return best, best_hits


def parse_study_plan_table(table, years: YearGuesser, normalize: Callable[[object], str] = None,
                           min_rows: int = MIN_DATE_ROWS, refuse_header: Callable = None) -> list:
    """One extracted table -> study sessions, or [] when it is not a plan.

    `min_rows` drops to 1 once the document has already shown a plan: its
    last page often holds a table of only two dates (the real upload did)."""
    normalize = normalize or (lambda c: _squash("" if c is None else str(c)))
    rows = [[normalize(c) for c in row] for row in (table or [])]
    rows = [r for r in rows if any(r)]
    if len(rows) < min_rows:
        return []
    date_col, hits = _date_column(rows)
    if date_col < 0 or hits < min_rows:
        return []
    first_date_row = next(i for i, r in enumerate(rows) if date_col < len(r) and read_plan_date(r[date_col]))
    body = rows[first_date_row:]
    # Most rows of the body must start with a date (or continue the row
    # above with an empty date cell); a table that merely mentions a few
    # dates in passing is something else.
    dated = sum(1 for r in body if date_col < len(r) and read_plan_date(r[date_col]))
    blank = sum(1 for r in body if date_col >= len(r) or not r[date_col])
    if dated + blank < 0.8 * len(body):
        return []

    header = rows[first_date_row - 1] if first_date_row > 0 else []
    width = max(len(r) for r in body)
    header = (header + [""] * width)[:width]
    if any(REFUSE_HEADER_RE.search(h) for h in header):
        return []
    # The rows above the first date (title, header) as main.py's exam and
    # class readers would see them.
    if refuse_header and refuse_header(table[:first_date_row]):
        return []

    # Order of day and month for numeric dates: the table decides once.
    numeric = [read_plan_date(r[date_col]) for r in body if date_col < len(r)]
    numeric = [d for d in numeric if d and d[3] == "numeric"]
    month_first = any(d[1] > 12 and d[0] <= 12 for d in numeric)
    if not month_first and any(d[1] > 12 for d in numeric):
        return []

    roles = {}
    for col in range(width):
        if col == date_col:
            continue
        h = header[col]
        values = [r[col] for r in body if col < len(r) and r[col]]
        if IGNORE_HEADER_RE.match(h) or DATE_HEADER_RE.match(h) and all(_is_weekday_cell(v) for v in values):
            roles[col] = "ignore"
        elif values and all(_is_weekday_cell(v) for v in values):
            roles[col] = "ignore"
        elif TIME_HEADER_RE.match(h) or (values and sum(_is_time_cell(v) for v in values) >= 0.8 * len(values)):
            roles[col] = "time"
        elif SUBJECT_HEADER_RE.match(h):
            roles[col] = "subject"
        elif TOPIC_HEADER_RE.match(h):
            roles[col] = "topic"
        elif not values:
            roles[col] = "ignore"
        else:
            roles[col] = "wide"
    subject_cols = [c for c, r in roles.items() if r == "subject"]
    topic_cols = [c for c, r in roles.items() if r == "topic"]
    time_cols = [c for c, r in roles.items() if r == "time"]
    wide_cols = [c for c, r in roles.items() if r == "wide"]
    long_format = bool(subject_cols or topic_cols)
    if long_format:
        # A plain extra column in a long table is the topic when none was named.
        topic_cols = topic_cols or wide_cols[:1]
        if not topic_cols and subject_cols:
            topic_cols, subject_cols = subject_cols[:1], []

    out = []
    current = None  # (iso date, start, end) of the row being read
    last_by_col = {}
    for r in body:
        cell = r[date_col] if date_col < len(r) else ""
        parsed = read_plan_date(cell) if cell else None
        if parsed:
            day, month, year, kind = parsed
            if kind == "numeric" and month_first:
                day, month = month, day
            if month > 12:
                continue
            found = years.place(day, month, year)
            if not found:
                current = None
                continue
            start, end = _read_times(cell)
            for c in time_cols:
                if c < len(r) and r[c]:
                    start, end = _read_times(r[c])
                    break
            current = (found.isoformat(), start, end)
            last_by_col = {}
        elif cell or current is None:
            current = None if cell else current
            if current is None:
                continue
        is_continuation = not parsed

        def emit(col, subject, topic):
            topic = _squash(topic)
            if not topic or is_day_off(topic):
                return
            if is_continuation and col in last_by_col:
                entry = last_by_col[col]
                entry["topic"] = _squash(entry["topic"] + " " + topic)[:MAX_TOPIC_CHARS]
                return
            if is_continuation:
                return
            entry = {
                "date": current[0],
                "subject": _squash(subject)[:60],
                "topic": topic[:MAX_TOPIC_CHARS],
                "start_time": current[1],
                "end_time": current[2],
            }
            out.append(entry)
            last_by_col[col] = entry

        if long_format:
            subject = next((r[c] for c in subject_cols if c < len(r) and r[c]), "")
            for c in topic_cols:
                if c < len(r):
                    emit(c, subject, r[c])
        else:
            for c in wide_cols:
                if c < len(r):
                    emit(c, header[c], r[c])

    dates = {e["date"] for e in out}
    if min_rows > 1 and (len(out) < MIN_DATE_ROWS or len(dates) < 2):
        return []
    return out


PAGE_YEAR_RE = re.compile(r"\b(20[2-4]\d)\b")


class StudyPlanReader:
    """Per-request page reader for _extract_records_sync. Keeps the year and
    the last date across pages, so page 2 of a plan that began on page 1 in
    December goes on into January rather than back to the start."""

    def __init__(self, today: Callable[[], date], extract: Callable, normalize: Callable = None,
                 refuse: Callable = None):
        self.years = YearGuesser(today)
        self.refuse = refuse or (lambda table: False)
        self.extract = extract
        self.normalize = normalize
        self.found = False

    def __call__(self, page) -> list:
        text = page.extract_text() or ""
        head = text[:400]
        m = PAGE_YEAR_RE.search(head)
        if m:
            self.years.hint(int(m.group(1)))
        out = []
        for table in self.extract(page):
            min_rows = 1 if self.found else MIN_DATE_ROWS
            rows = parse_study_plan_table(table, self.years, self.normalize, min_rows, self.refuse)
            self.found = self.found or bool(rows)
            out.extend(rows)
        return out
