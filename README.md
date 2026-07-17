# Focusity PDF Schedule Parser

Small rule-based FastAPI microservice that extracts exam-schedule tables from
university PDF files (Hebrew) and returns structured JSON — no LLM/AI calls,
just `pdfplumber` table extraction + Hebrew keyword header matching.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Test it:

```bash
curl -F "file=@/path/to/schedule.pdf" http://localhost:8000/parse-pdf
```

## How the parsing heuristic works

1. `pdfplumber` extracts every table on every page (`page.extract_tables()`).
2. For each table, the first few rows are scanned for cells that match a
   Hebrew keyword (`main.py:FIELD_KEYWORDS`) — e.g. `קורס`/`שם הקורס` for the
   course name, `תאריך` for the date, `שעה`/`משעה` for the start time,
   `עד שעה`/`סיום` for the end time. The row with the most keyword hits (at
   least 2) is treated as the header row, so a title row above the real table
   doesn't get mistaken for it.
3. Column indices are mapped dynamically from that header row — this is what
   lets the same service work across different universities/exports without
   hardcoding column positions.
4. Data rows are walked, dates normalized from `DD/MM/YYYY` (also accepts `.`
   or `-` separators, 2- or 4-digit years) to ISO `YYYY-MM-DD`, and times
   normalized to `HH:MM`. If a single time column holds a full `HH:MM-HH:MM`
   range, it's split into start/end automatically.
5. Rows missing a course name or a parseable date are dropped.

## Endpoint

`POST /parse-pdf` — multipart form upload, field name `file`, PDF only.

Response: `200` with a JSON array of exams:

```json
[
  {
    "course_name": "מבוא למדעי המחשב",
    "date": "2026-07-20",
    "start_time": "09:00",
    "end_time": "12:00",
    "room": "202",
    "moed": "א",
    "course_code": "83112"
  }
]
```

`room`, `moed`, and `course_code` are included only when a matching column
was found in the source PDF. `start_time`/`end_time` can be `null` if the
PDF doesn't expose a time column at all — the frontend review screen lets the
user fill those in by hand.

Errors return `4xx` with a Hebrew `detail` message (bad file type, empty
file, unreadable PDF, or no recognizable table found).

## Deploying to Render.com

This repo includes a `render.yaml` (Render "Blueprint"). To deploy:

1. Push this directory to its own GitHub repo (keep it separate from the
   Focusity frontend repo).
2. In Render: **New > Blueprint**, point it at the repo. Render will pick up
   `render.yaml` automatically (Python runtime, `pip install -r
   requirements.txt`, `uvicorn main:app --host 0.0.0.0 --port $PORT`).
3. Once deployed, copy the service URL (e.g.
   `https://focusity-pdf-parser.onrender.com`) into the Focusity frontend's
   `VITE_PDF_PARSER_URL` environment variable (Vercel project settings, plus
   local `.env`).

Note: Render's free plan spins down after inactivity, so the first request
after idle can take ~30-50s to cold-start — the frontend's upload UI should
show a "this can take a moment" state on first use.

## CORS

Open to all origins by default (`Access-Control-Allow-Origin: *`) since the
endpoint takes no cookies/auth and returns no user-specific data. Set the
`CORS_ALLOW_ORIGINS` env var to a comma-separated allowlist to restrict it.
