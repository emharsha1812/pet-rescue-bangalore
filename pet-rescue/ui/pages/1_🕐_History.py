"""Conversation History — past sessions with the Pet Rescue agent."""

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="History · Bengaluru Pet Rescue",
    page_icon="🕐",
    layout="wide",
)

HISTORY_FILE = Path(__file__).parent.parent / "history.json"


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return iso


def _preview(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = m.get("content", "")
            return text[:90] + "…" if len(text) > 90 else text
    return "Empty session"


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("🕐 Conversation History")
st.caption("All past sessions with the Bengaluru Pet Rescue agent, newest first.")

sessions = load_history()

if not sessions:
    st.info("No conversation history yet — start a chat on the main page!")
    st.stop()

sessions = sorted(sessions, key=lambda s: s.get("last_active", ""), reverse=True)

# ── Sidebar: stats + clear ────────────────────────────────────────────────────

with st.sidebar:
    st.metric("Total sessions", len(sessions))
    st.metric("Total messages", sum(len(s.get("messages", [])) for s in sessions))
    st.divider()
    if st.button("🗑️ Clear all history", type="secondary", use_container_width=True):
        HISTORY_FILE.unlink(missing_ok=True)
        st.success("History cleared.")
        st.rerun()

# ── Session list ──────────────────────────────────────────────────────────────

for i, session in enumerate(sessions):
    msgs = session.get("messages", [])
    n_user = sum(1 for m in msgs if m.get("role") == "user")
    preview = _preview(msgs)
    last = _fmt_dt(session.get("last_active", ""))

    label = f"{last}  ·  {n_user} exchange{'s' if n_user != 1 else ''}  ·  {preview}"

    with st.expander(label, expanded=(i == 0)):
        col_meta, col_id = st.columns([3, 5])
        col_meta.caption(f"Started: {_fmt_dt(session.get('started_at', ''))}")
        col_id.caption(f"Session ID: `{session['session_id']}`")
        st.divider()
        for msg in msgs:
            with st.chat_message(msg["role"]):
                st.markdown(msg.get("content", ""))
