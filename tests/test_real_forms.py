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
]


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


def test_grade_sheet_keeps_every_graded_course():
    rows = parse("parse-grade-sheet", "sce_grade_sheet.pdf")
    assert len(rows) == 24
    assert all(isinstance(r["grade"], float) for r in rows)


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
