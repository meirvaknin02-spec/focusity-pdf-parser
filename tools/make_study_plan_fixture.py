#!/usr/bin/env python
"""Builds tests/fixtures/study_plan_by_date.pdf.

    python tools/make_study_plan_fixture.py

Reproduces the SHAPE of a real upload (import_attempts 238b2bcf, 2026-11-05:
a day-by-day plan sent three times as a timetable, `no_table` each time, the
student then left with nothing). Every subject and topic here is made up;
only the structure is copied:

  - a heading with the date range and the year ("November 27 - December 4,
    2026"), the dates in the table carrying no year;
  - rows "Nov 27 (Fri)": month name, day, weekday in brackets;
  - one column per subject, the header cell naming it, a topic in each cell;
  - a whole row of HOLIDAY, and a single holiday cell beside real topics;
  - long topics that wrap onto a second line inside their cell;
  - the table running on to page 2, where the month turns into January
    with no year printed (the plan crosses the new year);
  - a second section on page 2 headed by its own title, dates without a
    weekday ("Jan 2").

ReportLab's Table, like the real file.
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

SUBJECTS = ["Biology", "History", "Statistics"]
PAGE1 = [
    ("Nov 27 (Fri)", "Cells and organelles", "The industrial revolution", "Mean, median and mode"),
    ("Nov 28 (Sat)", "Cell membrane transport", "Factory towns", "Variance"),
    ("Nov 29 (Sun)", "HOLIDAY", "HOLIDAY", "HOLIDAY"),
    ("Nov 30 (Mon)", "Photosynthesis - light reactions and the Calvin cycle in detail",
     "Railways", "Standard deviation"),
    ("Dec 1 (Tue)", "Respiration", "HOLIDAY", "Probability - 1"),
    ("Dec 2 (Wed)", "Revision 1: cells, membranes and photosynthesis", "Revision 1", "Probability - 2"),
]
PAGE2 = [
    ("Dec 30 (Wed)", "Genetics - 1", "The first world war", "Normal distribution"),
    ("Dec 31 (Thu)", "Genetics - 2", "Treaty of Versailles", "Sampling"),
    ("Jan 1 (Fri)", "NEW YEAR HOLIDAY", "NEW YEAR HOLIDAY", "NEW YEAR HOLIDAY"),
]
SECTION2 = [
    ("Jan 2", "Final biology revision", "Final history revision", "Mixed questions"),
    ("Jan 3", "Past paper", "Past paper", "Past paper"),
]

cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=9, leading=11)
head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=colors.white)
title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=15, leading=19)
sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=10, leading=13)


def table(rows):
    data = [[Paragraph(h, head) for h in ["Date"] + SUBJECTS]]
    data += [[Paragraph(c, cell) for c in r] for r in rows]
    t = Table(data, colWidths=[90, 220, 200, 200], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9AA5B1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5F8")]),
    ]))
    return t


def main():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / "study_plan_by_date.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), title="", author="", subject="", creator="")
    story = [
        Paragraph("SEMESTER 1 - STUDY SCHEDULE", title),
        Paragraph("November 27, 2026 - January 3, 2027 | Biology - History - Statistics", sub),
        Spacer(1, 10),
        table(PAGE1),
        PageBreak(),
        table(PAGE2),
        Spacer(1, 18),
        Paragraph("FINAL REVISION", title),
        Spacer(1, 6),
        table(SECTION2),
    ]
    doc.build(story)
    print(out)


if __name__ == "__main__":
    main()
