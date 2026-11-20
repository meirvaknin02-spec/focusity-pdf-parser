"""American class schedules (2026-11-20): 12-hour clocks, short English
headers, packed day cells. The fixture files are checked row by row in
test_real_forms.py; these name the failures that were seen, so a regression
says what broke."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main

HERE = Path(__file__).resolve().parent
client = TestClient(main.app)


def parse(fixture):
    with open(HERE / "fixtures" / fixture, "rb") as fh:
        response = client.post("/parse-class-schedule-pdf", files={"file": (fixture, fh, "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def meetings(rows):
    return {(r["course_name"], r["meeting_type"], r["day"], r["start_time"], r["end_time"]) for r in rows}


PROMO_WEEK = {
    ("Calculus I", "lecture", 1, "08:30", "10:00"),
    ("Intro to Computer Science", "lecture", 1, "10:30", "12:00"),
    ("Statistics", "lecture", 2, "09:00", "10:30"),
    ("Calculus I", "practice", 2, "12:00", "13:30"),
    ("Intro to Economics", "lecture", 3, "10:00", "11:30"),
    ("Intro to Computer Science", "lab", 3, "14:00", "15:30"),
    ("Academic Writing", "workshop", 4, "08:30", "10:00"),
    ("Statistics", "practice", 4, "12:00", "13:30"),
    ("Intro to Economics", "practice", 5, "09:00", "10:30"),
}


@pytest.mark.parametrize("fixture", [
    "us_class_table_ampm.pdf", "us_class_table_short_headers.pdf", "us_class_table_short_24h.pdf"])
def test_promo_timetable_reads_all_nine_classes_in_every_variant(fixture):
    rows = parse(fixture)
    assert len(rows) == 9
    assert meetings(rows) == PROMO_WEEK


def test_afternoon_pm_times_are_not_read_as_the_night():
    rows = parse("us_class_table_ampm.pdf")
    lab = next(r for r in rows if r["meeting_type"] == "lab")
    assert (lab["start_time"], lab["end_time"]) == ("14:00", "15:30")
    noon = [r for r in rows if r["start_time"] == "12:00"]
    assert len(noon) == 2 and all(r["end_time"] == "13:30" for r in noon)


def test_short_header_title_is_the_name_and_course_is_the_code():
    rows = parse("us_class_table_short_headers.pdf")
    assert all(r["end_time"] for r in rows)
    calc = [r for r in rows if r["course_name"] == "Calculus I"]
    assert len(calc) == 2 and all(r["course_code"] == "MATH 101" for r in calc)
    assert not any(r["course_name"].startswith(("MATH", "CS ", "STAT", "ECON", "ENG ")) for r in rows)


def test_packed_day_cells_become_one_meeting_per_day():
    rows = parse("us_registration_days.pdf")
    biology = sorted(r["day"] for r in rows if r["course_name"] == "Principles of Biology"
                     and r["meeting_type"] == "lecture")
    assert biology == [1, 3, 5]                      # MWF
    history = sorted(r["day"] for r in rows if r["course_name"] == "American History")
    assert history == [2, 4]                         # TR
    spanish = {(r["day"], r["start_time"], r["end_time"]) for r in rows if r["course_name"] == "Spanish II"}
    assert spanish == {(2, "12:00", "13:15"), (4, "12:00", "13:15")}  # Tue/Thu
    discussion = next(r for r in rows if r["meeting_type"] == "practice")
    assert (discussion["start_time"], discussion["end_time"]) == ("11:00", "12:15")  # 11:00-12:15 PM


@pytest.mark.parametrize("raw,want", [
    ("9:00 AM - 10:15 AM", ("09:00", "10:15")),
    ("12:00 PM - 1:30 PM", ("12:00", "13:30")),
    ("11:00-12:15 PM", ("11:00", "12:15")),
    ("9:00-9:50 AM", ("09:00", "09:50")),
    ("1:30PM - 2:45PM", ("13:30", "14:45")),
    ("1 PM to 2:30 PM", ("13:00", "14:30")),
    ("11:30 AM - 12:45", ("11:30", "12:45")),
    ("10:00 p.m. - 11:00 p.m.", ("22:00", "23:00")),
    ("17:00 -19:15", ("17:00", "19:15")),
    ("10-13", (None, None)),
    ("", (None, None)),
])
def test_clock_ranges(raw, want):
    assert main.read_clock_range(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("2:00 PM", "14:00"), ("12:00 PM", "12:00"), ("12:30 AM", "00:30"), ("9 am", "09:00"),
    ("14:00", "14:00"), ("08:30", "08:30"), ("", None),
])
def test_single_clock_readings(raw, want):
    assert main.read_clock(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("Monday", [1]), ("MWF", [1, 3, 5]), ("TR", [2, 4]), ("TTh", [2, 4]), ("MoWe", [1, 3]),
    ("Tue/Thu", [2, 4]), ("Mon, Wed", [1, 3]), ("Tues & Thurs", [2, 4]), ("F", [5]),
    ("ב", [1]), ("יום ג'", [2]), ("TBA", []), ("Online", []), ("", []),
])
def test_day_cells(raw, want):
    assert main.parse_class_days(raw) == want


@pytest.mark.parametrize("header,field", [
    ("End", "end_time"), ("End Time", "end_time"), ("Start", "start_time"), ("Time", "start_time"),
    ("Title", "course_name"), ("Course Title", "course_name"), ("Type", "meeting_type"),
    ("Course Type", "meeting_type"), ("Days", "day"), ("Instructor(s)", "lecturer"),
    ("Professor", "lecturer"), ("Attendance", None), ("Prototype", None),
    # Hebrew keeps matching anywhere in the cell; "סוג מקצוע" stays a course-ish column.
    ("סיום", "end_time"), ("שם הקורס", "course_name"), ("סוג מקצוע", "course_name"),
])
def test_header_words(header, field):
    assert main.match_class_field(header) == field


def test_course_title_header_keeps_the_code_column():
    mapping = main.map_class_columns(["Course", "Title", "Day", "Start", "End"], False)
    assert mapping["course_name"] == 1 and mapping["course_code"] == 0
    assert mapping["end_time"] == 4


@pytest.mark.parametrize("raw,want", [
    ("Lecture", "lecture"), ("LEC", "lecture"), ("Recitation", "practice"), ("DIS", "practice"),
    ("Lab", "lab"), ("Seminar", "workshop"), ("Required", ""), ("", ""),
])
def test_meeting_type_words(raw, want):
    assert main.meeting_type_word(raw) == want
