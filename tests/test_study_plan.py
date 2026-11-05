"""Day-by-day study plans (study_plan.py), on bare tables.

The real-file case is in test_real_forms.py (study_plan_by_date.pdf). These
pin the decisions that file cannot show on its own.
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import main
from study_plan import YearGuesser, is_day_off, parse_study_plan_table, read_plan_date

client = TestClient(main.app)
TODAY = date(2026, 11, 1)


def years():
    return YearGuesser(lambda: TODAY)


def test_a_timetable_is_not_a_study_plan():
    """The real upload this was built for, sent where it belongs: 422 with the
    plan's own message, and the timetable endpoint still refuses it too."""
    with open("tests/fixtures/study_plan_by_date.pdf", "rb") as fh:
        r = client.post("/parse-class-schedule-pdf", files={"file": ("p.pdf", fh, "application/pdf")})
    assert r.status_code == 422
    with open("tests/fixtures/sapir_timetable.pdf", "rb") as fh:
        r = client.post("/parse-study-plan-pdf", files={"file": ("p.pdf", fh, "application/pdf")})
    assert r.status_code == 422
    assert "תוכנית לימוד" in r.json()["detail"]


@pytest.mark.parametrize("fixture", ["sce_exam_schedule.pdf", "en_exam_list.pdf", "test_series_exams.pdf"])
def test_an_exam_list_is_never_read_as_a_study_plan(fixture):
    """Dated rows, a course per row: an exam list has the same bones. Read as a
    plan, the exams would land in the calendar as homework."""
    with open(f"tests/fixtures/{fixture}", "rb") as fh:
        r = client.post("/parse-study-plan-pdf", files={"file": (fixture, fh, "application/pdf")})
    assert r.status_code == 422


def test_plan_crosses_the_new_year_and_skips_holidays():
    rows = parse_study_plan_table([
        ["Date", "Math", "Physics"],
        ["Dec 30", "Limits", "Motion"],
        ["Dec 31", "HOLIDAY", "Forces"],
        ["Jan 1", "NEW YEAR HOLIDAY", "NEW YEAR HOLIDAY"],
        ["Jan 2", "Derivatives", "-"],
    ], years())
    assert [(r["date"], r["subject"], r["topic"]) for r in rows] == [
        ("2026-12-30", "Math", "Limits"), ("2026-12-30", "Physics", "Motion"),
        ("2026-12-31", "Physics", "Forces"), ("2027-01-02", "Math", "Derivatives"),
    ]


def test_long_plan_with_hebrew_dates_and_a_time_column():
    rows = parse_study_plan_table([
        ["תאריך", "שעה", "מקצוע", "נושא"],
        ["יום א' 8.11", "18:00-19:30", "סטטיסטיקה", "התפלגות נורמלית"],
        ["יום ב' 9.11", "17:00", "כלכלה", "היצע וביקוש"],
        ["10.11", "", "סטטיסטיקה", "חזרה"],
    ], years())
    assert rows == [
        {"date": "2026-11-08", "subject": "סטטיסטיקה", "topic": "התפלגות נורמלית", "start_time": "18:00", "end_time": "19:30"},
        {"date": "2026-11-09", "subject": "כלכלה", "topic": "היצע וביקוש", "start_time": "17:00", "end_time": None},
        {"date": "2026-11-10", "subject": "סטטיסטיקה", "topic": "חזרה", "start_time": None, "end_time": None},
    ]


def test_us_month_first_dates_are_read_month_first():
    rows = parse_study_plan_table([
        ["Date", "Task"],
        ["11/12/2026", "Essay outline"],
        ["11/13/2026", "Essay draft"],
        ["11/16/2026", "Essay final"],
    ], years())
    assert [r["date"] for r in rows] == ["2026-11-12", "2026-11-13", "2026-11-16"]
    assert all(r["subject"] == "" for r in rows)


def test_a_wrapped_cell_continues_its_row():
    """Borderless tables split a long topic over two extracted rows, the
    second with an empty date cell."""
    rows = parse_study_plan_table([
        ["Date", "Biology"],
        ["Nov 10", "Revision: cells and"],
        ["", "membranes"],
        ["Nov 11", "Genetics"],
        ["Nov 12", "Ecology"],
    ], years())
    assert rows[0]["topic"] == "Revision: cells and membranes"
    assert len(rows) == 3


def test_a_table_with_rooms_or_lecturers_is_refused():
    assert parse_study_plan_table([
        ["Date", "Course", "Room"],
        ["Nov 10", "Biology", "204"],
        ["Nov 11", "History", "105"],
        ["Nov 12", "Math", "301"],
    ], years()) == []


def test_a_document_mentioning_dates_in_passing_is_not_a_plan():
    assert parse_study_plan_table([
        ["Chapter", "Pages"],
        ["Chapter 3.2", "Read pages 10 to 20 and summarise them"],
        ["Chapter 4.1", "Read pages 21 to 30"],
        ["Due 12.11", "Hand in"],
    ], years()) == []


@pytest.mark.parametrize("cell", ["HOLIDAY", "DIWALI HOLIDAY", "Rest", "-", "חופש", "חג", "Day off"])
def test_day_off_cells(cell):
    assert is_day_off(cell)


@pytest.mark.parametrize("cell", ["Rest of chapter 3", "Break-even analysis", "Holiday homework: essay on the causes of the war"])
def test_topics_that_merely_contain_a_day_off_word(cell):
    assert not is_day_off(cell)


@pytest.mark.parametrize("cell,want", [
    ("Nov 6 (Fri)", (6, 11, None, "named")),
    ("6 November 2026", (6, 11, 2026, "named")),
    ("2026-11-06", (6, 11, 2026, "named")),
    ("יום ו' 6.11", (6, 11, None, "numeric")),
    ("6 בנובמבר", (6, 11, None, "named")),
    ("Introduction to costing", None),
    ("Chapter 3.2 exercises", None),
    ("10:30", None),
])
def test_read_plan_date(cell, want):
    assert read_plan_date(cell) == want


def test_first_date_without_a_year_is_the_coming_one():
    g = years()
    assert g.place(15, 6, None) == date(2027, 6, 15)  # more than four months back -> next year
    g = years()
    assert g.place(20, 10, None) == date(2026, 10, 20)  # a plan already under way
