#!/usr/bin/env python
"""Builds the American row-per-class timetables in tests/fixtures/:

    python tools/make_us_class_table_fixtures.py

us_class_table_ampm.pdf, us_class_table_short_headers.pdf and
us_class_table_short_24h.pdf reproduce the class schedule Focusity's English
promo films import (2026-11-20). Read before the fix, the 12-hour times came
out as night hours ("2:00 PM" -> 02:00, "12:00 PM - 1:30 PM" -> 01:30-12:00),
the short "Course | Title | ... | Start | End" header gave every row a code
for a name and no end time.

us_registration_days.pdf is the shape of a US registration print: one row
per section, every weekly day packed into one cell ("MWF", "TR", "MoWe",
"Tue/Thu") and the meeting as one "9:00-9:50 AM" range under "Time".

Every course, name and room here is made up.
"""
from pathlib import Path

import matplotlib
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FONT = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf" / "DejaVuSans.ttf"

ROWS = [
    ("MATH 101", "Calculus I", "Lecture", "Monday", "08:30", "10:00", "Dr. Carter", "Hall 104", "4"),
    ("CS 110", "Intro to Computer Science", "Lecture", "Monday", "10:30", "12:00", "Prof. Lee", "Eng 201", "4"),
    ("STAT 200", "Statistics", "Lecture", "Tuesday", "09:00", "10:30", "Dr. Brooks", "Sci 310", "3"),
    ("MATH 101", "Calculus I", "Recitation", "Tuesday", "12:00", "13:30", "Mr. Reed", "Hall 102", "4"),
    ("ECON 101", "Intro to Economics", "Lecture", "Wednesday", "10:00", "11:30", "Dr. Hayes", "Bus 105", "3"),
    ("CS 110", "Intro to Computer Science", "Lab", "Wednesday", "14:00", "15:30", "Ms. Patel", "Lab 2", "4"),
    ("ENG 120", "Academic Writing", "Seminar", "Thursday", "08:30", "10:00", "Ms. Green", "Hum 208", "3"),
    ("STAT 200", "Statistics", "Recitation", "Thursday", "12:00", "13:30", "Mr. Diaz", "Sci 310", "3"),
    ("ECON 101", "Intro to Economics", "Recitation", "Friday", "09:00", "10:30", "Ms. Ford", "Bus 105", "3"),
]
LONG_HEADS = ["Course Code", "Course Name", "Type", "Day", "Start Time", "End Time", "Instructor", "Room", "Credits"]
SHORT_HEADS = ["Course", "Title", "Type", "Day", "Start", "End", "Instructor", "Room", "Credits"]

REGISTRATION_HEADS = ["Subject", "Catalog", "Title", "Component", "Days", "Time", "Room", "Instructor"]
REGISTRATION_ROWS = [
    ("BIO", "150", "Principles of Biology", "LEC", "MWF", "9:00-9:50 AM", "Life Sci 120", "Dr. Kim"),
    ("BIO", "150", "Principles of Biology", "LAB", "Th", "2:00 PM - 4:50 PM", "Life Sci 014", "Ms. Ortiz"),
    ("HIST", "210", "American History", "LEC", "TR", "11:00 AM - 12:15 PM", "Hum 301", "Prof. Webb"),
    ("PSY", "101", "Intro to Psychology", "LEC", "MoWe", "1:30PM - 2:45PM", "Soc 110", "Dr. Shah"),
    ("PSY", "101", "Intro to Psychology", "DIS", "Fr", "11:00-12:15 PM", "Soc 004", "Mr. Lane"),
    ("SPAN", "102", "Spanish II", "LEC", "Tue/Thu", "12:00 PM-1:15 PM", "Lang 220", "Sra. Ruiz"),
]


def twelve_hour(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def build(name, heads, rows, title, col_widths=None):
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8ee")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
    ]
    table = Table([list(heads)] + [list(r) for r in rows], colWidths=col_widths)
    table.setStyle(TableStyle(style))
    out = FIXTURES / name
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), title="", author="", creator="")
    doc.build([Paragraph(title, ParagraphStyle("t", fontName="DejaVu", fontSize=14, leading=18)),
               Spacer(1, 6), table])
    print("wrote", out)


def with_clock(rows, clock):
    return [r[:4] + (clock(r[4]), clock(r[5])) + r[6:] for r in rows]


if __name__ == "__main__":
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT)))
    FIXTURES.mkdir(parents=True, exist_ok=True)
    title = "Class Schedule - Fall Semester 2026"
    build("us_class_table_ampm.pdf", LONG_HEADS, with_clock(ROWS, twelve_hour), title)
    build("us_class_table_short_headers.pdf", SHORT_HEADS, with_clock(ROWS, twelve_hour), title)
    build("us_class_table_short_24h.pdf", SHORT_HEADS, ROWS, title)
    build("us_registration_days.pdf", REGISTRATION_HEADS, REGISTRATION_ROWS, "Student Schedule - Spring 2027")
