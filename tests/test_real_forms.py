"""Real institutional exports, parsed end to end.

Each fixture is a real PDF a student uploaded, with every personal detail
removed (see tools/make_fixtures.py). The expected JSON is the output the
parser produced for it when it was known to be right, checked by hand against
the printed document. A change to main.py that alters any of it -- a row
lost, a date flipped, a course name mangled -- fails here before it can reach
Render.

When a new real file is fixed, add it the same way: redact it with
tools/make_fixtures.py, parse it, check the output against the PDF by eye,
and save that output to tests/expected/. Never regenerate an expected file
just to make a failing test pass; the failure is the point.
"""
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main

HERE = Path(__file__).resolve().parent
client = TestClient(main.app)

CASES = [
    # (endpoint, fixture, expected output, what this file taught us)
    ("parse-class-schedule-pdf", "sapir_timetable.pdf", "sapir_timetable.json",
     "calendar grid; lecture and practice split into meeting_type; mirrored brackets; credits drawn below the box"),
    ("parse-pdf", "sce_exam_schedule.pdf", "sce_exam_schedule.json",
     "ת.בחינה header, three-row header shifted one column, detached final letters, times missing"),
    ("parse-grade-sheet", "sce_grade_sheet.pdf", "sce_grade_sheet.json",
     "two pages, four semesters, 'השתתפ/ה' rows without a number"),
    ("parse-class-schedule-pdf", "sce_weekly_timetable.pdf", "sce_weekly_timetable.json",
     "Michlol weekly print: name column headed 'שעור' (no yod), the name one column left of its header"),
    # Drawn rather than redacted (tools/make_english_fixtures.py), because the
    # documents they reproduce belong to the universities that publish them.
    ("parse-class-schedule-pdf", "celcat_timetable.pdf", "celcat_timetable.json",
     "transposed grid: hours across the top, day names sideways, 12-hour headers, labelled cell lines"),
    ("parse-pdf", "en_exam_list.pdf", "en_exam_list.json",
     "English exam list: code-first lines, 12-hour times, and a duration column that must not become the start"),
    # Drawn (tools/make_partial_tounicode_fixture.py): the real Levinsky-Wingate
    # export carries a student's name, ID number and home address.
    ("parse-class-schedule-pdf", "partial_tounicode_timetable.pdf", "partial_tounicode_timetable.json",
     "Hebrew font whose /ToUnicode covers Latin only; letters live in the embedded cmap, nun twice"),
    # Read on a November date (see pin_today): semester A only.
    ("parse-class-schedule-pdf", "two_semester_timetable.pdf", "two_semester_timetable.json",
     "the whole year in one document, a banner row above each semester's table"),
    # Drawn (tools/make_dotted_range_fixture.py) in the shape of a real
    # Turkish upload that was rejected four times on 2026-10-16.
    ("parse-class-schedule-pdf", "dotted_range_timetable.pdf", "dotted_range_timetable.json",
     "Turkish day names, an hour column of '09.00 - 09.45' ranges, Latin codes, 'Derslik:' rooms"),
    # Drawn (tools/make_semester_pages_fixture.py) in the shape of a real
    # registration print rejected six times on 2026-10-23.
    ("parse-class-schedule-pdf", "semester_pages_timetable.pdf", "semester_pages_timetable.json",
     "one page per semester named in the heading, a 'שעות' column holding the time range"),
]


@pytest.fixture(autouse=True)
def pin_today(monkeypatch):
    """Which semester a whole-year timetable keeps depends on the date, so
    every test reads it on the same day unless it says otherwise."""
    monkeypatch.setattr(main, "_today", lambda: date(2026, 11, 1))


def parse(endpoint, fixture):
    with open(HERE / "fixtures" / fixture, "rb") as fh:
        response = client.post(f"/{endpoint}", files={"file": (fixture, fh, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("endpoint,fixture,expected,_why", CASES, ids=[c[1] for c in CASES])
def test_real_form_parses_exactly_as_expected(endpoint, fixture, expected, _why):
    got = parse(endpoint, fixture)
    want = json.loads((HERE / "expected" / expected).read_text(encoding="utf-8"))
    assert len(got) == len(want), f"{fixture}: {len(got)} rows, expected {len(want)}"
    for i, (g, w) in enumerate(zip(got, want)):
        assert g == w, f"{fixture} row {i + 1} changed:\n  got      {g}\n  expected {w}"


# Plain-language guards for the failures that actually reached students, so a
# regression names itself instead of showing up as a JSON diff.

def test_sce_exam_schedule_is_eleven_exams_not_one_garbled_row():
    rows = parse("parse-pdf", "sce_exam_schedule.pdf")
    assert len(rows) == 11
    assert rows[0]["course_name"] == "אמינות הנדסית"
    assert rows[0]["date"] == "2026-12-27" and rows[0]["start_time"] == "12:00"
    assert all(r["date"].startswith(("2026-", "2027-")) for r in rows)


def test_timetable_names_have_no_mirrored_brackets_and_types_are_split():
    rows = parse("parse-class-schedule-pdf", "sapir_timetable.pdf")
    assert not any(")" in r["course_name"] or "(" in r["course_name"] for r in rows)
    calculus = [r for r in rows if r["course_name"] == "קלקולוס 1"]
    assert sorted(r["meeting_type"] for r in calculus) == ["lecture", "practice"]


def test_timetable_credits_are_read_for_every_class_that_prints_them():
    rows = {r["course_name"]: r["credits"] for r in parse("parse-class-schedule-pdf", "sapir_timetable.pdf") if r["credits"] != ""}
    assert rows == {"התנהגות ארגונית": 3, "אנגלית בסיסית": 0, "יסודות החשבונאות": 3, "קלקולוס 1": 4}


def test_timetable_quarter_hour_blocks_keep_their_quarter_hour():
    """Sapir draws some class blocks on HALF rows of its 30-minute grid.

    Snapping each block to the nearest printed hour label once moved three of
    these ten classes a quarter of an hour early, as perfectly valid JSON with no
    warning - a student would simply turn up at 11:00 for a class that starts at
    11:15. The expected file had recorded that wrong output as correct, so the
    exact-match test above kept passing until the geometry was measured
    independently: block edges at rows 16.52, 22.52-25.52 and 6.52-9.52.
    """
    rows = parse("parse-class-schedule-pdf", "sapir_timetable.pdf")
    slots = {(r["day"], r["start_time"], r["end_time"]) for r in rows}
    assert (3, "14:30", "16:15") in slots
    assert (3, "19:15", "20:45") in slots
    assert (5, "11:15", "12:45") in slots
    # whole-row blocks are untouched by the half-row snap points
    assert (5, "08:30", "11:00") in slots
    assert all(r["start_time"][-2:] in ("00", "15", "30", "45") for r in rows)


def test_sce_weekly_print_reads_every_class_with_its_name():
    """SCE's Michlol "מערכת מפגשים לתקופה" print was rejected outright: its
    name column says "שעור" (no yod), and the name cell spans a blank-headed
    column to the left of that header, so even a matched header pointed at
    an empty column and every row was dropped as nameless."""
    rows = parse("parse-class-schedule-pdf", "sce_weekly_timetable.pdf")
    assert len(rows) == 11
    assert all(r["course_name"] and r["course_code"] for r in rows)
    assert rows[0]["course_name"] == "הנדסת איכות"
    assert (rows[0]["day"], rows[0]["start_time"], rows[0]["end_time"]) == (0, "08:00", "11:00")


def test_grade_sheet_keeps_every_graded_course():
    rows = parse("parse-grade-sheet", "sce_grade_sheet.pdf")
    assert len(rows) == 24
    assert all(isinstance(r["grade"], float) for r in rows)


def test_hebrew_letters_missing_from_tounicode_come_from_the_embedded_font():
    """Levinsky-Wingate's timetable names every Hebrew glyph only inside the
    embedded font, not in /ToUnicode, so all 26 classes were rejected as "no
    table" (every letter extracted as "(cid:N)"). The same font gives nun a
    second, private-use code point; picking that one silently dropped every
    נ - "התנועה" became "התועה", which still looks like a course name."""
    rows = parse("parse-class-schedule-pdf", "partial_tounicode_timetable.pdf")
    names = [r["course_name"] for r in rows]
    assert names[0] == "מבוא לתנועה ונשימה"
    assert not any("(cid:" in n or "" in n for n in names)
    # A Latin abbreviation reversed along with the Hebrew around it read "E.P.M".
    assert rows[0]["room"] == "M.P.E / מעבדה 101"


def test_whole_year_timetable_imports_only_the_running_semester(monkeypatch):
    """Levinsky-Wingate prints semester A and B in one document. Imported as one
    week, a course running in both came in twice and semester B's classes sat on
    top of semester A's on the same mornings."""
    def names_on(day):
        monkeypatch.setattr(main, "_today", lambda: day)
        return [r["course_name"] for r in parse("parse-class-schedule-pdf", "two_semester_timetable.pdf")]

    autumn = names_on(date(2026, 11, 1))
    assert len(autumn) == 5 and "תולדות הספורט" not in autumn
    assert autumn.count("מבוא לתנועה ונשימה") == 1

    spring = names_on(date(2027, 3, 15))
    assert spring == ["מבוא לתנועה ונשימה", "תולדות הספורט", "מבוא לאימון"]

    # No summer table in the document: in July the next semester is A.
    assert names_on(date(2027, 7, 10)) == autumn


def test_single_semester_documents_are_never_filtered(monkeypatch):
    monkeypatch.setattr(main, "_today", lambda: date(2027, 3, 15))
    assert len(parse("parse-class-schedule-pdf", "partial_tounicode_timetable.pdf")) == 5


def test_rows_before_the_first_semester_banner_are_kept():
    records = [{"n": 1}, {"n": 2, "_semester": "A"}, {"n": 3, "_semester": "B"}]
    assert [r["n"] for r in main.keep_running_semester(records)] == [1, 2]
    assert all("_semester" not in r for r in records)


# --- English exports -------------------------------------------------------
# Four real English timetables (Trinidad, India x2, New Zealand) all returned
# zero rows, which looked like a language problem and was not: day names and
# header keywords were already matched in both languages. They simply draw the
# same week the other way round, and print a 12-hour clock.

def test_transposed_grid_reads_as_one_students_week():
    rows = parse("parse-class-schedule-pdf", "celcat_timetable.pdf")
    slots = {(r["day"], r["start_time"], r["end_time"], r["course_name"]) for r in rows}
    assert (1, "11:00", "12:00", "MATH 1142") in slots
    # A class spanning two hour columns keeps its full length.
    assert (1, "15:00", "17:00", "SOWK 6000") in slots
    assert (3, "13:00", "14:00", "PHYS 0100") in slots


def test_transposed_grid_keeps_the_labelled_fields():
    rows = {r["course_name"]: r for r in parse("parse-class-schedule-pdf", "celcat_timetable.pdf")}
    assert rows["MATH 1142"]["lecturer"] == "TYAGI, Shikhar"
    # "Room: TLC" / "TR6" -- the value wraps onto the next line in a box this
    # narrow, and stopping at the label's own line lost half of every room.
    assert rows["MATH 1142"]["room"] == "TLC TR6"
    assert rows["PHYS 0100"]["lecturer"] == "SANKARAN, Amanda"
    assert rows["MATH 1142"]["meeting_type"] == "practice"
    assert rows["ELFU Grammar Workshop"]["meeting_type"] == "workshop"


def test_department_timetable_is_refused_rather_than_half_imported():
    """One row per programme inside each day band: the drawn boxes cover
    several programmes at once and nothing in the document says which one the
    student is on. Reporting that no timetable was recognized is recoverable;
    filling a week with other programmes' classes looks like it worked."""
    with open(HERE / "fixtures" / "department_timetable.pdf", "rb") as fh:
        response = client.post("/parse-class-schedule-pdf",
                               files={"file": ("x.pdf", fh, "application/pdf")})
    assert response.status_code == 422


def test_exam_duration_column_is_not_read_as_the_start_time():
    """"Saturday, 13 June 2026 9:00 AM 01:30" -- the 01:30 is how long the
    exam lasts. Collecting bare times into a sorted set put it first, so every
    row of a real Auckland timetable came out starting at its own length."""
    rows = {r["course_name"]: r for r in parse("parse-pdf", "en_exam_list.pdf")}
    assert rows["ACADENG 100"]["start_time"] == "09:00"
    assert rows["ACADENG 100"]["end_time"] == "10:30"
    # 2:00 PM is 14:00, not 02:00, and its duration still gives the end.
    assert rows["ACCTG 102"]["start_time"] == "14:00"
    assert rows["ACCTG 102"]["end_time"] == "16:30"
    assert rows["PHYSICS 220"]["start_time"] == "18:00"


def test_exam_course_name_is_the_course_not_the_whole_row():
    """Campus, permitted materials and invigilation mode all used to end up
    inside the course name, which made the review table unreadable."""
    names = {r["course_name"] for r in parse("parse-pdf", "en_exam_list.pdf")}
    assert names == {"ACADENG 100", "ACCTG 102", "COMPSCI 399", "PHYSICS 220"}


@pytest.mark.parametrize("fixture", [c[1] for c in CASES])
def test_fixtures_carry_no_personal_details(fixture):
    """The repository is public. This fails if an un-redacted export is ever committed."""
    import pdfplumber
    with pdfplumber.open(HERE / "fixtures" / fixture) as pdf:
        raw = "\n".join((p.extract_text() or "") for p in pdf.pages)
    text = raw + "\n" + "\n".join(main.fix_bidi_line(line) for line in raw.split("\n"))
    import re
    assert not re.search(r"(?<!\d)\d{9}(?!\d)", text), "a 9-digit ID number is present"
    assert not re.search(r"(?<!\d)05\d{8}(?!\d)", text), "a mobile phone number is present"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an e-mail address is present"


def test_registration_print_with_a_range_hours_column_and_a_page_per_semester(monkeypatch):
    """A student uploaded this print six times and got "no timetable" every
    time: its "שעות" column holds "17:00 -19:15" (read as weekly hours, so no
    start time), and "סוג מקצוע" sits before "שם הקורס", so courses would have
    been named after their type. Each semester is its own page, named only in
    the heading."""
    rows = parse("parse-class-schedule-pdf", "semester_pages_timetable.pdf")
    assert [r["course_name"] for r in rows] == ["מבוא לכלכלה", "סדנת כתיבה", "יסודות החשבונאות", "סטטיסטיקה"]
    assert (rows[0]["day"], rows[0]["start_time"], rows[0]["end_time"]) == (0, "17:00", "19:15")
    assert rows[0]["room"] == "זום"

    monkeypatch.setattr(main, "_today", lambda: date(2027, 3, 15))
    assert [r["course_name"] for r in parse("parse-class-schedule-pdf", "semester_pages_timetable.pdf")] == [
        "שיווק דיגיטלי", "עולם העבודה"]


def test_range_hour_column_ends_a_class_on_its_last_rows_printed_end():
    """'12.00 - 12.45' then '12.45 - 13.30' run back to back while other rows
    leave a gap: a class ends at the end printed on its last row, never at the
    start of the row after it."""
    rows = {r["course_name"]: r for r in parse("parse-class-schedule-pdf", "dotted_range_timetable.pdf")}
    assert (rows["Mikrobiyoloji Uyg. A"]["start_time"], rows["Mikrobiyoloji Uyg. A"]["end_time"]) == ("12:00", "13:30")
    assert (rows["Biyokimya II"]["start_time"], rows["Biyokimya II"]["end_time"]) == ("10:00", "11:45")
    assert rows["Biyokimya II"]["day"] == 1  # Pazartesi = Monday
    assert rows["Biyokimya II"]["course_code"] == "BIO2201050"
    assert rows["Biyokimya II"]["room"] == "204"
