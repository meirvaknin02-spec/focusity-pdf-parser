#!/usr/bin/env python
"""Builds the English exam-schedule tables in tests/fixtures/:

    python tools/make_us_exam_schedule_fixtures.py

us_final_exams.pdf is an American final-exam schedule (2026-11-23): a bare
"Course" holding the code, "Title" holding the name, month-first dates
("12/14/2026") and 12-hour "Start"/"End" columns. Before the fix every row
was dropped (no 14th month), so the import failed outright.

us_final_exams_ambiguous.pdf has only dates that read both ways (12/8 ..
12/11) and one "Exam Time" range per row; its 12-hour times make it
American, so 12/08/2026 is 8 December.

uk_exam_timetable.pdf is the day-first counterpart: English headers, the same
ambiguous-looking dates, 24-hour times. It must stay day-first (08/12/2026 is
8 December there too, written the other way round).

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

FINALS_HEADS = ["Course", "Title", "Date", "Start", "End", "Room"]
FINALS = [
    ("MATH 1172", "Calculus II", "12/14/2026", "8:00 AM", "9:45 AM", "Hitchcock 131"),
    ("CHEM 1210", "General Chemistry I", "12/15/2026", "12:00 PM", "1:45 PM", "McPherson 1000"),
    ("CSE 2231", "Data Structures", "12/16/2026", "4:00 PM", "5:45 PM", "Dreese 264"),
    ("PSYCH 1100", "Intro to Psychology", "12/17/2026", "10:00 AM", "11:45 AM", "Mendenhall 100"),
]

AMBIGUOUS_HEADS = ["Course Title", "Exam Date", "Exam Time", "Location"]
AMBIGUOUS = [
    ("Calculus II", "12/08/2026", "8:00 AM - 9:45 AM", "Hall 131"),
    ("General Chemistry I", "12/09/2026", "12:00 PM - 1:45 PM", "Hall 1000"),
    ("Data Structures", "12/10/2026", "4:00 PM - 5:45 PM", "Lab 264"),
    ("Intro to Psychology", "12/11/2026", "10:00 AM - 11:45 AM", "Hall 100"),
]

UK_HEADS = ["Course Title", "Exam Date", "Start Time", "End Time", "Room"]
UK = [
    ("Calculus II", "08/12/2026", "09:00", "11:00", "Hall A"),
    ("General Chemistry I", "09/12/2026", "13:30", "15:30", "Hall B"),
    ("Data Structures", "10/12/2026", "09:00", "11:00", "Hall A"),
]


def build(name, heads, rows, title):
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8ee")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
    ]
    table = Table([list(heads)] + [list(r) for r in rows])
    table.setStyle(TableStyle(style))
    out = FIXTURES / name
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), title="", author="", creator="")
    doc.build([Paragraph(title, ParagraphStyle("t", fontName="DejaVu", fontSize=14, leading=18)),
               Spacer(1, 6), table])
    print("wrote", out)


if __name__ == "__main__":
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT)))
    FIXTURES.mkdir(parents=True, exist_ok=True)
    build("us_final_exams.pdf", FINALS_HEADS, FINALS, "Final Exam Schedule - Autumn 2026")
    build("us_final_exams_ambiguous.pdf", AMBIGUOUS_HEADS, AMBIGUOUS, "Final Exams - Fall 2026")
    build("uk_exam_timetable.pdf", UK_HEADS, UK, "Examination Timetable - Michaelmas Term 2026")
