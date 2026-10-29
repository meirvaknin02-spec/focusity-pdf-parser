#!/usr/bin/env python
"""Builds tests/fixtures/spanish_period_timetable.pdf and
tests/fixtures/test_series_exams.pdf.

    python tools/make_period_and_test_series_fixtures.py

Both reproduce the structure of real uploads from 2026-10-29. Every subject,
test and date here is made up; only the shape is copied.

spanish_period_timetable.pdf -- a Chilean school timetable exported from
Google Sheets (import_attempts 24605c0e, rejected three times, then 42
classes typed in by hand):
  - Spanish day names ("Horas | Lunes | Martes | Miércoles | ...");
  - one ROW per period with its own range ("8:10 - 8:55"), the subject
    repeated in each day column, no box per class -- Sheets paints a stripe
    across the whole row instead;
  - the afternoon on a 12-hour clock with no PM ("12:35 - 1:20");
  - empty break rows, and lunch typed one letter per day column, "L U N C H".

test_series_exams.pdf -- an Indian JEE coaching "test series" schedule
(import_attempts 37b97c3f): one row per test with "Test Type" naming the
sitting ("MINOR TEST - 1"), "Test Timings*" as "1 PM-4 PM" or "1-SHIFT", and
two continuation rows per test that list the other subjects. Read before the
fix, every test came out named "Physics" with no time.
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

PERIODS = [
    ("7:45 - 8:00", ["Consejo"] * 5),
    ("8:00 - 8:45", ["Historia", "Química", "Inglés", "Música", "Historia"]),
    ("8:45 - 9:30", ["Historia", "Química", "Inglés", "Música", "Biología"]),
    ("9:30 - 9:50", [""] * 5),
    ("9:50 - 10:35", ["Física", "Lenguaje", "Álgebra", "Inglés", "Lenguaje"]),
    ("10:35 - 11:20", ["Física", "Lenguaje", "Álgebra", "Inglés", "Lenguaje"]),
    ("11:20 - 12:05", ["Álgebra", "Historia", "Física", "Taller", "Química"]),
    ("12:05 - 12:50", ["Álgebra", "Historia", "Lenguaje", "Taller", "Química"]),
    ("12:50 - 1:35", ["L", "U", "N", "C", "H"]),
    ("1:35 - 2:20", ["Biología", "Ed. Física", "Química", "Física", "Arte"]),
    ("2:20 - 3:05", ["Biología", "Ed. Física", "Química", "Física", "Arte"]),
]

TESTS = [
    ("1", "05/07/2026", "Sunday", "UNIT TEST - 1", "1-SHIFT", "Physics", "Units, Vectors"),
    ("2", "26/07/2026", "Sunday", "UNIT TEST - 2", "2 PM-5 PM", "Physics", "Kinematics"),
    ("3", "09/08/2026", "Sunday", "CUMULATIVE TEST - 1", "9 AM-12 PM", "Physics", "Tests 1 and 2"),
    ("4", "23/08/2026", "Sunday", "UNIT TEST - 3", "2 PM-5 PM", "Physics", "Laws of Motion"),
]


def _font():
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT)))
    return ParagraphStyle("title", fontName="DejaVu", fontSize=14, leading=18)


def build_period_timetable(title):
    data = [["Horas", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]]
    data += [[hours] + subjects for hours, subjects in PERIODS]
    stripes = ["#ffffff", "#f6f8f9", "#d9ead3"]
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f19bcd")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b5749a")),
    ]
    for i in range(1, len(data)):
        style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(stripes[i % 3])))
    table = Table(data, colWidths=[115] * 6, rowHeights=19)
    table.setStyle(TableStyle(style))
    out = FIXTURES / "spanish_period_timetable.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), title="", author="", creator="")
    doc.build([Paragraph("Horario 2° Medio", title), Spacer(1, 6), table])
    print("wrote", out)


def build_test_series(title):
    data = [["Test No.", "Test Date", "Test Day", "Test Type", "Test Timings*", "Subject", "Topics Covered"]]
    spans = []
    for no, day, weekday, kind, timing, subject, topics in TESTS:
        first = len(data)
        data.append([no, day, weekday, kind, timing, subject, topics])
        data.append(["", "", "", "", "", "Chemistry", "Mole Concept"])
        data.append(["", "", "", "", "", "Mathematics", "Sets, Functions"])
        spans += [("SPAN", (c, first), (c, first + 2)) for c in range(5)]
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ] + spans
    table = Table(data, colWidths=[45, 70, 50, 120, 75, 75, 300])
    table.setStyle(TableStyle(style))
    out = FIXTURES / "test_series_exams.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), title="", author="", creator="")
    doc.build([Paragraph("Test Series : Topic Wise Syllabus & Schedule", title), Spacer(1, 6), table])
    print("wrote", out)


if __name__ == "__main__":
    style = _font()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    build_period_timetable(style)
    build_test_series(style)
