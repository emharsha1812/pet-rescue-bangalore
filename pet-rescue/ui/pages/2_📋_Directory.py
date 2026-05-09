"""Animal Organisation Directory — fetched live from S3."""

import io
import urllib.parse
from datetime import datetime

import boto3
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()  # pick up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from .env

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Organisation Directory · Bengaluru Pet Rescue",
    page_icon="📋",
    layout="wide",
)

S3_BUCKET = "animal-directory"
S3_KEY = "AnimalDirectory.xlsx"

# Columns that might contain opening-hour ranges like "9:00 AM - 6:00 PM"
HOURS_COL_CANDIDATES = ["hours", "opening hours", "timings", "timing", "working hours", "time"]

# ── S3 fetch ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def _load_directory() -> pd.DataFrame:
    """Download the Excel from S3 and return a DataFrame."""
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
    data = obj["Body"].read()
    df = pd.read_excel(io.BytesIO(data), engine="openpyxl")
    df.columns = df.columns.str.strip()
    pin_col = next((c for c in df.columns if "pin" in c.lower()), None)
    if pin_col:
        df[pin_col] = df[pin_col].apply(
            lambda v: str(int(float(v))) if pd.notna(v) and str(v).replace(".", "").isdigit() else ""
        )
    return df


def _find_hours_col(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if col.strip().lower() in HOURS_COL_CANDIDATES:
            return col
    return None


# ── Open-now logic ────────────────────────────────────────────────────────────

DAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_ALWAYS_OPEN = {"24/7", "open 24 hours", "24 hours", "always open"}


def _expand_days(day_expr: str) -> list[int]:
    """'Mon-Fri', 'Sat, Sun', or 'Mon' → list of weekday ints (0=Mon … 6=Sun)."""
    result = []
    for part in day_expr.split(","):
        part = part.strip().lower()
        if "-" in part:
            sub = part.split("-", 1)
            start = DAY_MAP.get(sub[0].strip())
            end = DAY_MAP.get(sub[1].strip())
            if start is not None and end is not None:
                if end >= start:
                    result.extend(range(start, end + 1))
                else:  # wraps e.g. Fri-Mon
                    result.extend(range(start, 7))
                    result.extend(range(0, end + 1))
        elif part in DAY_MAP:
            result.append(DAY_MAP[part])
    return result


def _parse_time_range(time_str: str) -> tuple[int, int] | None:
    """'9:00 AM - 6:00 PM' or '09:00 - 18:00' → (open_mins, close_mins). None if unparseable."""
    if not isinstance(time_str, str):
        return None
    raw = time_str.strip()
    if not raw or raw.lower() in _ALWAYS_OPEN:
        return None
    for sep in (" - ", " – ", "–", " to "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
                try:
                    open_t = datetime.strptime(left.strip(), fmt)
                    close_t = datetime.strptime(right.strip(), fmt)
                    return open_t.hour * 60 + open_t.minute, close_t.hour * 60 + close_t.minute
                except ValueError:
                    continue
    return None


def _check_range(open_m: int, close_m: int, current: int) -> bool:
    if close_m < open_m:  # spans midnight
        return current >= open_m or current < close_m
    return open_m <= current < close_m


def _is_open_now(cell_value) -> bool | None:
    """
    Parses multi-line hours. Supported cell formats:

        Mon-Fri: 9:00 AM - 6:00 PM
        Sat: 10:00 AM - 4:00 PM
        Sun: Closed

    Also handles:
        - Single time range with no day prefix  →  applied every day
        - '24/7' or 'Open 24 hours'            →  always True
        - 'Closed'                              →  False
        - Empty / missing                       →  None (shown as '— verify')
    """
    if cell_value is None:
        return None
    if isinstance(cell_value, float) and pd.isna(cell_value):
        return None
    text = str(cell_value).strip()
    if not text or text.lower() in ("nan", "none", "-", "n/a", ""):
        return None

    if text.lower() in _ALWAYS_OPEN:
        return True
    if text.lower() == "closed":
        return False

    now = datetime.now()
    today_wd = now.weekday()  # 0=Mon, 6=Sun
    current = now.hour * 60 + now.minute

    lines = [l.strip() for l in text.replace(";", "\n").splitlines() if l.strip()]

    for line in lines:
        if ":" not in line:
            continue
        # Split on first colon — day specs never contain colons, times do ("9:00")
        colon_idx = line.index(":")
        day_part = line[:colon_idx].strip()
        time_part = line[colon_idx + 1:].strip()

        days = _expand_days(day_part)
        if not days:
            continue  # colon was inside a time string, not a day prefix
        if today_wd not in days:
            continue

        # Found a rule for today
        if time_part.lower() == "closed":
            return False
        if time_part.lower() in _ALWAYS_OPEN:
            return True
        parsed = _parse_time_range(time_part)
        if parsed is None:
            return None
        return _check_range(*parsed, current)

    # No day-prefixed lines matched — treat the whole cell as a plain time range
    parsed = _parse_time_range(text)
    if parsed is None:
        return None
    return _check_range(*parsed, current)


def _open_now_html(cell_value) -> str:
    status = _is_open_now(cell_value)
    if status is True:
        return "<span style='color:#1a7f3c;font-weight:600;font-size:14px'>● Open</span>"
    if status is False:
        return "<span style='color:#c0392b;font-weight:600;font-size:14px'>● Closed</span>"
    # No hours data — show neutral badge
    return "<span style='color:#888;font-size:13px'>— verify</span>"


# ── Render ────────────────────────────────────────────────────────────────────

st.title("📋 Animal Organisation Directory")
st.caption("Live data from S3 · refreshes every 5 minutes · all Bengaluru-based orgs")

with st.spinner("Fetching directory from S3…"):
    try:
        df = _load_directory()
        load_error = None
    except Exception as exc:
        df = pd.DataFrame()
        load_error = str(exc)

if load_error:
    st.error(f"Could not load directory from S3: `{load_error}`")
    st.stop()

if df.empty:
    st.warning("The directory spreadsheet appears to be empty.")
    st.stop()

hours_col = _find_hours_col(df)

# ── Filter sidebar ────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filter")
    search = st.text_input("Search organisations", placeholder="e.g. CUPA, rescue, cat…")

    area_options = ["All"]
    for col in ["Area / Locality", "Area", "Locality", "Location"]:
        if col in df.columns:
            areas = sorted(df[col].dropna().unique().tolist())
            area_options += areas
            break
    selected_area = st.selectbox("Area / Locality", area_options)

    focus_col = None
    for col in ["Focus Areas", "Focus", "Services", "Service"]:
        if col in df.columns:
            focus_col = col
            break
    if focus_col:
        focus_search = st.text_input("Focus area keyword", placeholder="e.g. rescue, TNR, adoption…")
    else:
        focus_search = ""

    show_open_only = False
    if hours_col:
        show_open_only = st.checkbox("Open now only")

# ── Apply filters ─────────────────────────────────────────────────────────────

filtered = df.copy()

if search:
    mask = filtered.apply(
        lambda row: row.astype(str).str.contains(search, case=False, na=False).any(), axis=1
    )
    filtered = filtered[mask]

if selected_area != "All":
    for col in ["Area / Locality", "Area", "Locality", "Location"]:
        if col in filtered.columns:
            filtered = filtered[filtered[col].astype(str).str.lower() == selected_area.lower()]
            break

if focus_search and focus_col:
    filtered = filtered[
        filtered[focus_col].astype(str).str.contains(focus_search, case=False, na=False)
    ]

if show_open_only and hours_col:
    filtered = filtered[filtered[hours_col].apply(lambda v: _is_open_now(v) is True)]

# ── Metric strip ──────────────────────────────────────────────────────────────

total = len(df)
showing = len(filtered)
open_count = sum(1 for v in df.get(hours_col, pd.Series([])) if _is_open_now(v) is True) if hours_col else None

m1, m2, m3 = st.columns(3)
m1.metric("Total organisations", total)
m2.metric("Showing", showing)
if open_count is not None:
    m3.metric("Open right now", open_count)

st.divider()

# ── Build HTML table ──────────────────────────────────────────────────────────

# Identify website, maps, name, and address columns for link rendering
website_col = next((c for c in filtered.columns if "website" in c.lower()), None)
maps_col = next((c for c in filtered.columns if "maps" in c.lower() or "google" in c.lower()), None)
name_col = next((c for c in filtered.columns if "organisation" in c.lower() or c.lower() == "name"), None)
addr_col = next((c for c in filtered.columns if "address" in c.lower()), None)


def _maps_url(row: pd.Series) -> str:
    """Build a Google Maps search URL from org name + address."""
    parts = []
    if name_col and pd.notna(row.get(name_col)):
        parts.append(str(row[name_col]).strip())
    if addr_col and pd.notna(row.get(addr_col)):
        parts.append(str(row[addr_col]).strip())
    query = " ".join(parts) if parts else "Bengaluru"
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"


def _cell_html(col: str, value) -> str:
    """Render a single table cell's inner HTML."""
    if pd.isna(value):
        return "<span style='color:#bbb'>—</span>"
    s = str(value).strip()
    if not s:
        return "<span style='color:#bbb'>—</span>"
    if col == website_col and s.startswith("http"):
        domain = s.split("/")[2] if "/" in s else s
        return f"<a href='{s}' target='_blank' style='color:#1a73e8'>{domain}</a>"
    if col == maps_col:
        # maps column is handled per-row in the loop below — return placeholder
        return ""
    return s


# Columns to display (drop hours col; add OpenNow at the end)
display_cols = [c for c in filtered.columns if c != hours_col]

header_cells = "".join(f"<th>{c}</th>" for c in display_cols)
header_cells += "<th>Open Now</th>"

rows_html = ""
for _, row in filtered.iterrows():
    cell_parts = []
    for col in display_cols:
        if col == maps_col:
            url = _maps_url(row)
            inner = f"<a href='{url}' target='_blank' style='color:#1a73e8'>📍 Open in Maps</a>"
        else:
            inner = _cell_html(col, row[col])
        cell_parts.append(
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;vertical-align:top'>{inner}</td>"
        )
    cells = "".join(cell_parts)
    open_html = _open_now_html(row.get(hours_col) if hours_col else None)
    cells += (
        f"<td style='padding:8px 12px;border-bottom:1px solid #eee;"
        f"vertical-align:top;text-align:center'>{open_html}</td>"
    )
    rows_html += f"<tr>{cells}</tr>"

table_html = f"""
<div style='overflow-x:auto;border-radius:8px;border:1px solid #e0e0e0;margin-top:8px'>
<table style='width:100%;border-collapse:collapse;font-size:14px;font-family:sans-serif'>
  <thead>
    <tr style='background:#1a1a2e;color:white'>
      {"".join(f"<th style='padding:10px 12px;text-align:left;white-space:nowrap'>{c}</th>" for c in display_cols)}
      <th style='padding:10px 12px;text-align:center;white-space:nowrap'>Open Now</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</div>
"""

if filtered.empty:
    st.info("No organisations match the current filters.")
else:
    st.markdown(table_html, unsafe_allow_html=True)

st.caption(f"Last fetched: {datetime.now().strftime('%d %b %Y, %I:%M %p')}  ·  Source: s3://{S3_BUCKET}/{S3_KEY}")
