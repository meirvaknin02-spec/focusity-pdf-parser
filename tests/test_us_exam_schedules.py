"""English exam schedules (2026-11-23): an American final-exam schedule
failed to import at all -- month-first dates were read day-first and every
row had an impossible 14th month. Fixtures: tools/make_us_exam_schedule_fixtures.py."""
from pathlib import Path

from fastapi.testclient import TestClient

import main

HERE = Path(__file__).resolve().parent
client = TestClient(main.app)


def parse(fixture):
    with open(HERE / "fixtures" / fixture, "rb") as fh:
        response = client.post("/parse-pdf", files={"file": (fixture, fh, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def rows(records):
    return [(r["course_name"], r["date"], r["start_time"], r["end_time"]) for r in records]


def test_us_final_exams_read_every_row_with_names_codes_and_pm_times():
    records = parse("us_final_exams.pdf")
    assert rows(records) == [
        ("Calculus II", "2026-12-14", "08:00", "09:45"),
        ("General Chemistry I", "2026-12-15", "12:00", "13:45"),
        ("Data Structures", "2026-12-16", "16:00", "17:45"),
        ("Intro to Psychology", "2026-12-17", "10:00", "11:45"),
    ]
    assert [r["course_code"] for r in records] == ["MATH 1172", "CHEM 1210", "CSE 2231", "PSYCH 1100"]
    assert records[0]["room"] == "Hitchcock 131"


def test_ambiguous_dates_in_an_american_table_are_month_first():
    assert rows(parse("us_final_exams_ambiguous.pdf")) == [
        ("Calculus II", "2026-12-08", "08:00", "09:45"),
        ("General Chemistry I", "2026-12-09", "12:00", "13:45"),
        ("Data Structures", "2026-12-10", "16:00", "17:45"),
        ("Intro to Psychology", "2026-12-11", "10:00", "11:45"),
    ]


def test_a_british_table_stays_day_first():
    assert rows(parse("uk_exam_timetable.pdf")) == [
        ("Calculus II", "2026-12-08", "09:00", "11:00"),
        ("General Chemistry I", "2026-12-09", "13:30", "15:30"),
        ("Data Structures", "2026-12-10", "09:00", "11:00"),
    ]


def test_normalize_date_orders():
    assert main.normalize_date("03/04/2027") == "2027-04-03"
    assert main.normalize_date("03/04/2027", "mdy") == "2027-03-04"
    # Impossible one way, valid the other: read the valid way.
    assert main.normalize_date("12/14/2026") == "2026-12-14"
    assert main.normalize_date("14/12/2026", "mdy") == "2026-12-14"
