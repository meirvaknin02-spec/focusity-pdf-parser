#!/usr/bin/env python
"""Builds the English-export fixtures in tests/fixtures/.

    python tools/make_english_fixtures.py

Unlike make_fixtures.py, which redacts a real student's upload, these are
drawn from scratch -- the documents they imitate are published by
universities and are not ours to redistribute. Each one reproduces the
*structure* that broke the parser, measured from a real file:

  celcat_timetable.pdf    A weekly grid the other way round: hours across
                          the top, day names written sideways down the left
                          edge, 12-hour clock headers, and each class a box
                          of labelled lines (Course:/Staff:/Room:). This is
                          the CELCAT layout, which most UK, Irish and
                          Commonwealth universities export.

  department_timetable.pdf The same shape, but one row per programme inside
                          each day -- a department-wide document, not one
                          student's week. Every box covers several
                          programmes at once, so the only honest outcome is
                          to refuse the file.

  en_exam_list.pdf        An exam timetable as text lines: course code first,
                          a 12-hour start time, and a DURATION column that a
                          naive reader mistakes for the start.

Checked against: University of the West Indies Semester 1 2026/2027 room
timetable, IIT Bhubaneswar Autumn 2026 1st-semester B.Tech timetable, and
the University of Auckland Semester 1 2026 examination timetable, all read
on 2026-10-01.
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

HOURS = ["08:00AM", "09:00AM", "10:00AM", "11:00AM", "12:00PM",
         "01:00PM", "02:00PM", "03:00PM", "04:00PM", "05:00PM"]
# reportlab measures y from the bottom of the page; everything here is laid
# out from the top, so the page height is carried explicitly.
PAGE_W, PAGE_H = landscape(A4)
LEFT, HEADER_Y, COL_W, ROW_H = 90, PAGE_H - 60, 60, 150


def _hour_header(c):
    c.setFont("Helvetica", 8)
    for i, label in enumerate(HOURS):
        c.drawCentredString(LEFT + i * COL_W + COL_W / 2, HEADER_Y, label)


def _row_top(row):
    return HEADER_Y - 20 - row * ROW_H


def _day_label(c, text, row):
    """Sideways down the left edge, the way the real exports print it."""
    c.saveState()
    c.translate(LEFT - 25, _row_top(row) - ROW_H / 2)
    c.rotate(90)
    c.setFont("Helvetica", 11)
    c.drawCentredString(0, 0, text)
    c.restoreState()


def _class_box(c, row, first_col, span, lines):
    """One class. The box is sized to its own text so no line falls outside
    it -- a line drawn past the border is not read as part of the cell, which
    is correct behaviour and a confusing way to write a fixture."""
    height = 12 + 9 * len(lines)
    x = LEFT + first_col * COL_W + 2
    y_top = _row_top(row)
    c.setStrokeColorRGB(0.2, 0.4, 0.8)
    c.rect(x, y_top - height, span * COL_W - 4, height)
    c.setFont("Helvetica", 7)
    for i, line in enumerate(lines):
        c.drawString(x + 3, y_top - 11 - i * 9, line)


def celcat_timetable(path):
    c = canvas.Canvas(str(path), pagesize=landscape(A4))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(LEFT, PAGE_H - 30, "Semester 1 TT2026_2027 - Room timetable")
    _hour_header(c)
    for row, day in enumerate(("Monday", "Tuesday", "Wednesday")):
        _day_label(c, day, row)
    # Monday 11:00-12:00, one column.
    _class_box(c, 0, 3, 1, ["Tutorial, Wks W5-", "W12 [=8]", "Course:",
                            "MATH 1142", "Staff:", "TYAGI, Shikhar", "Room: TLC", "TR6"])
    # Monday 15:00-17:00, two columns: the span has to survive.
    _class_box(c, 0, 7, 2, ["Lecture, Wks W5-W12 [=8]", "Course: SOWK 6000",
                            "Room: TLC TR6"])
    # Tuesday 09:00-10:00.
    _class_box(c, 1, 1, 1, ["Workshop, Wks", "W5-W12 [=8]", "Course:",
                            "ELFU Grammar", "Workshop", "Room: TLC", "TR6"])
    # Wednesday 13:00-14:00, with a lecturer whose name wraps.
    _class_box(c, 2, 5, 1, ["Lecture, Wks", "W5-W12 [=8]", "Course:",
                            "PHYS 0100", "Staff:", "SANKARAN,", "Amanda",
                            "Room: TLC", "TR6"])
    c.save()


def department_timetable(path):
    """One row per programme inside each day: the boxes overlap in time, so
    no single week can be read out of it."""
    c = canvas.Canvas(str(path), pagesize=landscape(A4))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(LEFT, PAGE_H - 30, "Class Time Table, 1st Sem B.Tech. Programmes")
    _hour_header(c)
    for row, day in enumerate(("Monday", "Tuesday", "Wednesday")):
        _day_label(c, day, row)
    for row in range(3):
        for offset, programme in enumerate(("CE", "CSE", "ECE")):
            _class_box(c, row, 2, 3,
                       [f"CODE (L) {programme}", "R12M/R17M/R22M/R27M"])
            # Stack the programmes inside the same day band.
            c.translate(0, -24)
        c.translate(0, 72)
    c.save()


EXAM_ROWS = [
    ("ACADENG 100", "Online", "Saturday, 13 June 2026", "9:00 AM", "01:30"),
    ("ACCTG 102*", "City", "Thursday, 18 June 2026", "2:00 PM", "02:30"),
    ("COMPSCI 399", "City", "Monday, 22 June 2026", "11:30 AM", "03:00"),
    ("PHYSICS 220", "Grafton", "Friday, 26 June 2026", "6:00 PM", "02:00"),
]


def en_exam_list(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, 790, "University Semester 1 2026 Examination Timetable")
    c.setFont("Helvetica", 9)
    c.drawString(50, 770, "Exam Campus Date Start Duration Permitted Materials")
    for i, (code, campus, date, start, duration) in enumerate(EXAM_ROWS):
        c.drawString(50, 750 - i * 16,
                     f"{code} {campus} {date} {start} {duration} Open Book "
                     f"/Calculators Permitted Mode A - Remote online exam")
    c.save()


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    celcat_timetable(FIXTURES / "celcat_timetable.pdf")
    department_timetable(FIXTURES / "department_timetable.pdf")
    en_exam_list(FIXTURES / "en_exam_list.pdf")
    print("wrote 3 fixtures to", FIXTURES)
