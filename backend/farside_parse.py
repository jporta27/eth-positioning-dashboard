"""Farside Investors ETF-flow HTML/CSV parsing helpers.

Shared by the live backend (backend/main.py) and the bulk backfill script
(scripts/backfill.py). Both used to have divergent implementations; this
module is the single source of truth.

Parser is defensive: iterates every <table> in the HTML, picks the one whose
header row has the most uppercase 3–5-letter ticker codes, and collects
date rows regardless of table position. This makes it resilient to Farside
reshuffling the page layout.

Public API:
    parse_farside_csv(text)            -> dict | None
    parse_farside_html(html)           -> dict | None
    parse_sosovalue(payload)           -> dict | None
    parse_farside_number(s)            -> float | None
    looks_like_date(s)                 -> bool
    sort_etf_rows_ascending(daily)     -> list
    parse_farside_date_to_epoch(s)     -> int  (seconds; 0 if unparseable)
"""

from __future__ import annotations

import csv
import io
import re
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("dashboard")

_ROW_PAT    = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_PAT   = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.DOTALL | re.IGNORECASE)
_TICKER_RE  = re.compile(r"^[A-Z]{3,5}$")
_TABLE_PAT  = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
_TAG_STRIP  = re.compile(r"<[^>]+>")


def parse_farside_number(s) -> Optional[float]:
    """Parse a Farside cell:
        '123.4' -> 123.4
        '(45.6)' -> -45.6  (accounting-style negative)
        '-', '—' -> 0.0    (reported as no flow)
        '', 'N/A', 'n/a' -> 0.0  (empty cell; Farside treats blanks as no-flow)
        None -> None       (only an explicit None propagates)
        '1,234.5' -> 1234.5
    """
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("$", "")
    if not s or s in ("-", "—", "N/A", "n/a"):
        return 0.0
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def looks_like_date(s: str) -> bool:
    """Heuristic: does s look like ISO/DMY/'10 Jan 2025' shape?"""
    if not s:
        return False
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return True
    if re.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$", s):
        return True
    if re.match(r"^\d{1,2}\s+[A-Za-z]{3,}\s+\d{2,4}$", s):
        return True
    return False


def parse_farside_date_to_epoch(s: str) -> int:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def sort_etf_rows_ascending(daily: list) -> list:
    def _key(row):
        try:
            return parse_farside_date_to_epoch(row["date"])
        except Exception:
            return 0
    return sorted(daily, key=_key)


def parse_farside_csv(text: str) -> Optional[dict]:
    """Parse a Farside CSV (columns: Date, <issuers…>, Total). Sometimes
    published at /wp-content/uploads/ETH.csv; returns None if layout doesn't
    match. Not relied upon today — Farside rarely serves the CSV."""
    try:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if len(rows) < 2:
            return None
        header = [h.strip() for h in rows[0]]
        if not header or header[0].lower() not in ("date", ""):
            return None
        issuers = header[1:-1]
        daily = []
        for row in rows[1:]:
            if len(row) != len(header):
                continue
            date_str = row[0].strip()
            if not date_str or date_str.lower().startswith("total") or "seed" in date_str.lower():
                continue
            flows = {iss: parse_farside_number(v) for iss, v in zip(issuers, row[1:-1])}
            total = parse_farside_number(row[-1])
            daily.append({"date": date_str, "byIssuer": flows, "total": total})
        if not daily:
            return None
        return {"daily": sort_etf_rows_ascending(daily), "issuers": issuers}
    except Exception as e:
        logger.warning(f"Farside CSV parse failed: {e}")
        return None


def parse_farside_html(html: str) -> Optional[dict]:
    """Parse a Farside HTML page.

    Iterates every <table>, identifies the issuer-header row as the row with
    the most uppercase 3–5-letter tickers, and collects date rows from the
    table with the most date rows. Robust to new tables / page restructuring.
    """
    try:
        html_clean = html.replace("&nbsp;", " ")
        tables = _TABLE_PAT.findall(html_clean)
        if not tables:
            return None

        def _rows(table_html):
            return [
                [_TAG_STRIP.sub("", c).strip() for c in _CELL_PAT.findall(rm.group(1))]
                for rm in _ROW_PAT.finditer(table_html)
            ]

        best = None  # (date_row_count, daily_list, issuers_list)
        for table_html in tables:
            rows = _rows(table_html)
            if not rows:
                continue

            # Find the issuer-header row: maximum count of ticker-like cells
            issuers: list = []
            best_hit = 0
            for cells in rows:
                tickers = [c for c in cells
                           if _TICKER_RE.match(c) and c not in ("TOTAL", "DATE", "ETF", "USD")]
                if len(tickers) >= 3 and len(tickers) > best_hit:
                    best_hit = len(tickers)
                    issuers = tickers

            # Collect date rows (they come after the header)
            daily = []
            for cells in rows:
                if len(cells) < 3:
                    continue
                date_str = cells[0]
                if not looks_like_date(date_str):
                    continue
                body = cells[1:]
                flows = {}
                if issuers:
                    for j, iss in enumerate(issuers):
                        v = body[j] if j < len(body) else ""
                        flows[iss] = parse_farside_number(v)
                # Total = rightmost non-empty body cell, or sum of per-issuer flows
                total = None
                for v in reversed(body):
                    if v and v not in ("-", "—"):
                        total = parse_farside_number(v)
                        if total is not None:
                            break
                if total is None and flows:
                    vals = [v for v in flows.values() if v is not None]
                    total = round(sum(vals), 2) if vals else None
                daily.append({"date": date_str, "byIssuer": flows, "total": total})

            if best is None or len(daily) > best[0]:
                best = (len(daily), daily, issuers)

        if not best or not best[1]:
            return None
        daily, issuers = best[1], best[2]
        return {"daily": sort_etf_rows_ascending(daily), "issuers": issuers}
    except Exception as e:
        logger.warning(f"Farside HTML parse failed: {e}")
        return None


def parse_sosovalue(payload) -> Optional[dict]:
    """Parse SoSoValue historicalInflowChart response. Best-effort — their
    public schema changes; kept as a tertiary fallback."""
    try:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") or payload.get("result") or payload
        items = data.get("list", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        daily = []
        for item in items:
            if not isinstance(item, dict):
                continue
            date_str = item.get("date") or item.get("time") or item.get("timestamp")
            val = item.get("inflow") or item.get("netInflow") or item.get("value")
            try:
                total = float(val) / 1e6 if val is not None else None  # USD → M$
            except (TypeError, ValueError):
                total = None
            if date_str and total is not None:
                daily.append({"date": str(date_str), "byIssuer": {}, "total": round(total, 2)})
        if not daily:
            return None
        return {"daily": sort_etf_rows_ascending(daily), "issuers": []}
    except Exception as e:
        logger.warning(f"SoSoValue parse failed: {e}")
        return None
