#!/usr/bin/env python
"""Builds tests/fixtures/dotted_range_timetable.pdf.

    python tools/make_dotted_range_fixture.py

Reproduces the structure of a real student's upload (import_attempts
57cb296c, 2026-10-16: four tries, `no_table` every time, then 161 classes
typed in by hand). Every course, code and room here is made up; only the
shape is copied:

  - day names in Turkish (Pazartesi .. Cuma), which matched nothing;
  - the hour column printed as RANGES with a dot, "09.00 - 09.45" -- three
    words, none of which looked like a clock time to the grid reader;
  - irregular rows (12.00 - 12.45 and 12.45 - 13.30 run back to back, other
    rows leave a 15-minute gap), so a class's end is the end printed on the
    last row it covers, not the start of the next row;
  - each class a filled cell spanning one or more rows, holding the name, a
    Latin course code and a labelled room line ("Derslik: 204").

The real file was itself made with ReportLab's Table, so this draws the same
way. The font is DejaVu Sans (free licence), shipped with matplotlib, because
the standard Helvetica has no ı or ş.
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

DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma"]
HOURS = ["09.00 - 09.45", "10.00 - 10.45", "11.00 - 11.45", "12.00 - 12.45", "12.45 - 13.30",
         "13.30 - 14.15", "14.30 - 15.15", "15.30 - 16.15", "16.30 - 17.15"]
# (day column, first row, last row, text, fill)
CLASSES = [
    (0, 1, 2, "Biyokimya II\nBIO2201050\nDerslik: 204", "#dcebfa"),
    (1, 0, 0, "Anatomi I\nANA1102030\nDerslik: 115", "#e3f4dd"),
    (1, 5, 6, "Fizyoloji\nFZY2103070\nDerslik: 204", "#ebddf4"),
    (2, 3, 4, "Mikrobiyoloji Uyg. A\nMIK2104010", "#fce8d5"),
    (3, 6, 8, "Histoloji Laboratuvarı\nHIS2105020\nDerslik: Lab 3", "#dcebfa"),
    (4, 0, 2, "İstatistik\nIST2106040\nDerslik: 310", "#fadce3"),
]


def build():
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT)))
    cell = ParagraphStyle("cell", fontName="DejaVu", fontSize=9, leading=12)
    title = ParagraphStyle("title", fontName="DejaVu", fontSize=16, leading=20)

    data = [[""] + DAYS] + [[h] + [""] * len(DAYS) for h in HOURS]
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4f63")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#e8eef2")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b0bec5")),
    ]
    for col, first, last, text, fill in CLASSES:
        data[first + 1][col + 1] = Paragraph(text.replace("\n", "<br/>"), cell)
        style.append(("SPAN", (col + 1, first + 1), (col + 1, last + 1)))
        style.append(("BACKGROUND", (col + 1, first + 1), (col + 1, last + 1), colors.HexColor(fill)))

    table = Table(data, colWidths=[80] + [148] * len(DAYS), rowHeights=[26] + [52] * len(HOURS))
    table.setStyle(TableStyle(style))
    note = Paragraph("Perşembe: laboratuvar dersleri (A grubu).", cell)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / "dotted_range_timetable.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), leftMargin=10, rightMargin=10,
                            topMargin=24, bottomMargin=10, title="", author="", creator="")
    doc.build([Paragraph("Ders Programı — Sınıf II", title), Spacer(1, 4), table, Spacer(1, 6), note])
    print("wrote", out)


if __name__ == "__main__":
    build()
