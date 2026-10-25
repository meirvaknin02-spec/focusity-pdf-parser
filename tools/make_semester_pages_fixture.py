#!/usr/bin/env python
"""Builds tests/fixtures/semester_pages_timetable.pdf.

    python tools/make_semester_pages_fixture.py

Reproduces the structure of a real student's upload (import_attempts
512844d2, 2026-10-23: the same file six times, `no_table` every time, then
the student exported the schedule to Excel by hand). Every course, lecturer
and room here is made up; only the shape is copied:

  - an iTextSharp registration print, one page per semester, the semester
    named only in the page heading ("סמסטר : א'"), never in the table;
  - "ק" for the summer semester;
  - the time column headed "שעות" (elsewhere the weekly-hours header) and
    holding the whole range, "17:00 -19:15", with no separate end column;
  - "סוג מקצוע" (the subject TYPE) to the right of "שם הקורס", and "קורס"
    holding the course NUMBER, so the leftmost course-ish header is wrong;
  - the room under "מקום";
  - Hebrew drawn in VISUAL order, as the real file is, so the reader has to
    detect and undo the reversal.

The font is David Libre (SIL Open Font Licence).
"""
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main import fix_bidi_line  # noqa: E402  (logical -> visual is the same run reversal)

FIXTURES = ROOT / "tests" / "fixtures"
FONT = Path(r"C:/Windows/Fonts/DavidLibre-Regular.ttf")

# Logical order, right to left on the page: day first.
HEADER = ["יום", "שעות", "קורס", "שם הקורס", "סוג מקצוע", "מרצה", "מקום", 'נ"ז']
# (semester letter, rows) -- row = day, time range, number, name, type, lecturer, room, credits
PAGES = [
    ("א", [
        ("א", "17:00 -19:15", "1101", "מבוא לכלכלה", "חובה", "ד\"ר כהן", "זום", "3.0"),
        ("ג", "15:00 -16:30", "1102", "סדנת כתיבה", "בחירה", "ד\"ר לוי", "302", ""),
        ("ג", "16:45 -18:15", "1103", "יסודות החשבונאות", "חובה", "פרופ' מזרחי", "253", "2.0"),
        ("ו", "08:15 -13:00", "1104", "סטטיסטיקה", "חובה", "ד\"ר אברהם", "512", "3.0"),
    ]),
    ("ב", [
        ("א", "17:15 -19:30", "1201", "שיווק דיגיטלי", "חובה", "ד\"ר כהן", "זום", "3.0"),
        ("ג", "16:00 -17:30", "1202", "עולם העבודה", "בחירה", "פרופ' לוי", "513", "2.0"),
    ]),
    ("ק", [
        ("ג", "15:30 -17:00", "1301", "היבטים משפטיים", "חובה", "ד\"ר פרץ", "זום", "2.0"),
    ]),
]


def visual(text: str) -> str:
    return fix_bidi_line(text)


def build():
    pdfmetrics.registerFont(TTFont("David", str(FONT)))
    head = ParagraphStyle("head", fontName="David", fontSize=11, leading=15, alignment=2)

    story = []
    for letter_, rows in PAGES:
        for line in ("מערכת שעות", f"סמסטר : {letter_}'"):
            story.append(Paragraph(visual(line), head))
        story.append(Spacer(1, 8))
        # Drawn left to right, so each row is reversed: the day ends up rightmost.
        data = [[visual(h) for h in reversed(HEADER)]]
        data += [[visual(c) for c in reversed(row)] for row in rows]
        table = Table(data, colWidths=[40, 50, 80, 60, 110, 70, 75, 30])
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "David"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ]))
        story += [table, PageBreak()]
    story.pop()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / "semester_pages_timetable.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=letter, title="", author="", creator="")
    doc.build(story)
    print("wrote", out)


if __name__ == "__main__":
    build()
