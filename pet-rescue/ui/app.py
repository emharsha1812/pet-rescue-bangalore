"""Bengaluru Pet Rescue & Adoption Coordinator — Streamlit UI."""

import io
import json
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

# ── Config ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Bengaluru Pet Rescue",
    page_icon="🐾",
    layout="wide",
)

API_BASE = "http://localhost:8000"

# Named locations for the picker — mirrors backend/prompts.py fallback table
LOCATIONS: dict[str, tuple[float, float]] = {
    "Forum Mall (Koramangala)": (12.9347, 77.6101),
    "Indiranagar": (12.9719, 77.6412),
    "Whitefield": (12.9698, 77.7500),
    "Jayanagar": (12.9250, 77.5938),
    "HSR Layout": (12.9116, 77.6473),
    "Bangalore center": (12.9716, 77.5946),
}

# Used by extract_location() for text-based place detection
PLACE_COORDS: dict[str, tuple[float, float]] = {
    "forum mall": (12.9347, 77.6101),
    "koramangala": (12.9347, 77.6101),
    "indiranagar": (12.9719, 77.6412),
    "whitefield": (12.9698, 77.7500),
    "jayanagar": (12.9250, 77.5938),
    "hsr layout": (12.9116, 77.6473),
    "hsr": (12.9116, 77.6473),
    "malleswaram": (13.0035, 77.5710),
    "sadashivanagar": (13.0050, 77.5710),
    "btm layout": (12.9166, 77.6101),
    "btm": (12.9166, 77.6101),
    "jp nagar": (12.9100, 77.5938),
    "vasanth nagar": (12.9855, 77.5965),
}
BANGALORE_CENTER = (12.9716, 77.5946)
BANGALORE_BBOX = {"lat": (12.8, 13.1), "lon": (77.4, 77.8)}

DEMO_PROMPTS = [
    ("🐶 Adopt a calm dog", "Looking for a calm cuddly dog good with my toddler"),
    ("🍼 Show young puppies", "Show puppies under 3 months across all shelters"),
]

HISTORY_FILE = Path(__file__).parent / "history.json"

# Folium icon colors — must be valid Folium color strings
ORG_COLORS = ["green", "purple", "orange", "cadetblue", "darkblue", "darkred", "darkgreen"]
ORG_HEX = {
    "green": "#28a745",
    "purple": "#6f42c1",
    "orange": "#fd7e14",
    "cadetblue": "#4e9fa3",
    "darkblue": "#003f88",
    "darkred": "#8b0000",
    "darkgreen": "#155724",
}

# ── Session state ─────────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "last_structured" not in st.session_state:
    st.session_state.last_structured = {
        "animals": [], "vets": [], "rescuers": [], "protocols": []
    }
if "response_count" not in st.session_state:
    st.session_state.response_count = 0
if "pending_prompt" not in st.session_state:
    st.session_state.pending_prompt = None
if "last_user_loc" not in st.session_state:
    st.session_state.last_user_loc = None
# Persistent user location — set by picker and/or browser geo
if "user_location" not in st.session_state:
    st.session_state.user_location = {
        "lat": 12.9347,
        "lon": 77.6101,
        "place_name": "Forum Mall (Koramangala)",
        "source": "manual",
    }
if "geo_attempted" not in st.session_state:
    st.session_state.geo_attempted = False
if "geo_raw" not in st.session_state:
    st.session_state.geo_raw = None   # {"lat": ..., "lon": ...} when in-bbox geo succeeds
if "geo_out_of_bbox" not in st.session_state:
    st.session_state.geo_out_of_bbox = False
if "picker_status" not in st.session_state:
    st.session_state.picker_status = None  # None | "ok" | "not_found" | "out_of_bbox"
if "picker_query" not in st.session_state:
    initial = st.session_state.user_location.get("place_name", "")
    st.session_state.picker_query = "" if "Auto-detected" in initial else initial

# ── Backend helpers ───────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def _fetch_health_cached() -> dict:
    # Raises on failure so st.cache_data never stores a bad result
    r = requests.get(f"{API_BASE}/health", timeout=5)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def fetch_health() -> dict | None:
    try:
        return _fetch_health_cached()
    except Exception:
        return None


def call_chat(message: str) -> tuple[str | None, dict | None]:
    try:
        payload = {
            "message": message,
            "session_id": st.session_state.session_id,
            "user_location": st.session_state.user_location,
        }
        r = requests.post(f"{API_BASE}/chat", json=payload, timeout=60)
        if r.status_code == 200:
            data = r.json()
            return data["reply"], data["structured_results"]
        return f"Backend error (HTTP {r.status_code}) — please try again.", None
    except requests.Timeout:
        return "Request timed out (60 s) — the agent is taking too long. Try again.", None
    except requests.ConnectionError:
        return "Backend unreachable — check that uvicorn is running on port 8000.", None
    except Exception as exc:
        return f"Unexpected error: {exc}", None


def _save_to_history(session_id: str, messages: list[dict]) -> None:
    """Persist the current session to history.json. Non-critical — silently ignores errors."""
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
        now = datetime.now(timezone.utc).isoformat()
        for entry in data:
            if entry["session_id"] == session_id:
                entry["messages"] = messages
                entry["last_active"] = now
                break
        else:
            data.append({
                "session_id": session_id,
                "started_at": now,
                "last_active": now,
                "messages": messages,
            })
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def extract_location(text: str) -> tuple[float, float] | None:
    lower = text.lower()
    for place, coords in PLACE_COORDS.items():
        if place in lower:
            return coords
    return None


@st.cache_data(ttl=3600)
def geocode_area(area: str) -> tuple[float, float] | None:
    """Return (lat, lon) from Nominatim for an area in Bengaluru, or None."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"{area}, Bengaluru, Karnataka, India",
                "format": "json",
                "limit": 1,
            },
            headers={"User-Agent": "bengaluru-pet-rescue/1.0 (demo)"},
            timeout=5,
        )
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600)
def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return a human-readable neighbourhood name for GPS coords via Nominatim, or None."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 14},
            headers={"User-Agent": "bengaluru-pet-rescue/1.0 (demo)"},
            timeout=5,
        )
        data = r.json()
        addr = data.get("address", {})
        for field in ("suburb", "neighbourhood", "quarter", "city_district"):
            if field in addr:
                return addr[field]
        display = data.get("display_name", "")
        return display.split(",")[0].strip() or None
    except Exception:
        return None


# ── Directory (S3) helpers ────────────────────────────────────────────────────

DIR_BUCKET = "animal-directory"
DIR_KEY    = "AnimalDirectory.xlsx"


@st.cache_data(ttl=300, show_spinner=False)
def load_directory() -> pd.DataFrame:
    """Fetch AnimalDirectory.xlsx from S3 and return as a DataFrame."""
    s3  = boto3.client("s3")
    obj = s3.get_object(Bucket=DIR_BUCKET, Key=DIR_KEY)
    df  = pd.read_excel(io.BytesIO(obj["Body"].read()), engine="openpyxl")
    df.columns = df.columns.str.strip()
    # Normalise PIN Code column to plain strings ("560034.0" → "560034")
    pin_col = next((c for c in df.columns if "pin" in c.lower()), None)
    if pin_col:
        df[pin_col] = df[pin_col].apply(
            lambda v: str(int(float(v))) if pd.notna(v) and str(v).replace(".", "").isdigit() else ""
        )
    return df


def filter_directory(df: pd.DataFrame, pincode: str = "", area: str = "", n: int = 5) -> pd.DataFrame:
    """Return up to n rows nearest to the given pincode, falling back to area name."""
    pin_col  = next((c for c in df.columns if "pin"      in c.lower()), None)
    area_col = next((c for c in df.columns if "area"     in c.lower() or "locality" in c.lower()), None)

    pincode = pincode.strip()
    area    = area.strip()

    if pincode and pin_col:
        # 1. Exact pincode
        exact = df[df[pin_col] == pincode]
        if not exact.empty:
            return exact.head(n)
        # 2. First-5-digit prefix (adjacent pincodes)
        if len(pincode) >= 5:
            nearby = df[df[pin_col].str.startswith(pincode[:5])]
            if not nearby.empty:
                return nearby.head(n)

    if area and area_col:
        area_lower = area.lower()
        matched = df[df[area_col].astype(str).str.lower().str.contains(area_lower, na=False)]
        if not matched.empty:
            return matched.head(n)

    # Last resort: first n rows
    return df.head(n)


def format_dir_for_prompt(df: pd.DataFrame, label: str = "") -> str:
    """Format filtered directory rows as a compact text block for the agent prompt."""
    if df.empty:
        return ""

    name_col    = next((c for c in df.columns if "organisation" in c.lower() or c.lower() == "name"), None)
    focus_col   = next((c for c in df.columns if "focus"   in c.lower()), None)
    addr_col    = next((c for c in df.columns if "address" in c.lower()), None)
    area_col    = next((c for c in df.columns if "area"    in c.lower() or "locality" in c.lower()), None)
    pin_col     = next((c for c in df.columns if "pin"     in c.lower()), None)
    website_col = next((c for c in df.columns if "website" in c.lower()), None)

    header = f"VERIFIED NEARBY ORGANISATIONS{' (PIN ' + label + ')' if label else ''}:"
    lines  = [header]

    for i, (_, row) in enumerate(df.iterrows(), 1):
        name = str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else "Unknown"
        focus   = str(row[focus_col]).strip()   if focus_col   and pd.notna(row.get(focus_col))   else ""
        addr    = str(row[addr_col]).strip()    if addr_col    and pd.notna(row.get(addr_col))    else ""
        area    = str(row[area_col]).strip()    if area_col    and pd.notna(row.get(area_col))    else ""
        pin     = str(row[pin_col]).strip()     if pin_col     and pd.notna(row.get(pin_col))     else ""
        website = str(row[website_col]).strip() if website_col and pd.notna(row.get(website_col)) else ""

        entry = [f"{i}. {name}"]
        if focus:
            entry.append(f"   Focus: {focus}")
        if addr:
            entry.append(f"   Address: {addr}")
        if area or pin:
            entry.append(f"   Area: {area}  PIN: {pin}".strip())
        if website:
            entry.append(f"   Website: {website}")
        lines.extend(entry)

    return "\n".join(lines)


# ── Map ───────────────────────────────────────────────────────────────────────

def build_map(
    structured: dict,
    user_location: dict | None,
    map_key: str,
) -> None:
    animals = structured.get("animals", [])
    vets = structured.get("vets", [])
    has_results = bool(animals or vets)

    if not has_results and not user_location:
        st.info("Ask a question to see results on the map.")
        return

    # Determine mode and result locations
    if vets:
        valid_locs = [v["location"] for v in vets if v.get("location")]
        mode = "emergency"
    elif animals:
        valid_locs = [a["location"] for a in animals if a.get("location")]
        mode = "adoption"
    else:
        valid_locs = []
        mode = None

    # Center: result centroid → user location → city center
    if valid_locs:
        center = (
            sum(loc["lat"] for loc in valid_locs) / len(valid_locs),
            sum(loc["lon"] for loc in valid_locs) / len(valid_locs),
        )
    elif user_location:
        center = (user_location["lat"], user_location["lon"])
    else:
        center = BANGALORE_CENTER

    m = folium.Map(location=center, zoom_start=13, tiles="CartoDB positron")

    # User pin — always shown in all modes
    if user_location:
        place_name = user_location.get("place_name", "Your location")
        folium.Marker(
            location=[user_location["lat"], user_location["lon"]],
            popup=folium.Popup(f"You — {place_name}", max_width=200),
            tooltip="You",
            icon=folium.Icon(color="blue", icon="home", prefix="fa"),
        ).add_to(m)

    if mode == "emergency":
        for vet in vets:
            loc = vet.get("location")
            if not loc:
                continue
            dist = vet.get("distance_km")
            dist_str = f"{dist} km away" if dist is not None else ""
            phone = vet.get("phone", "")
            popup_html = (
                f"<b>{vet.get('name', '')}</b><br>"
                f"{dist_str}<br>"
                f"<a href='tel:{phone}'>📞 {phone}</a><br>"
                f"{vet.get('address', '')}<br>"
                f"<span style='color:green;font-weight:bold'>✓ Open now</span>"
            )
            folium.Marker(
                location=[loc["lat"], loc["lon"]],
                popup=folium.Popup(popup_html, max_width=260),
                tooltip=vet.get("name", "Vet"),
                icon=folium.Icon(color="red", icon="plus", prefix="fa"),
            ).add_to(m)

    elif mode == "adoption":
        orgs = list(dict.fromkeys(a.get("source_org", "Unknown") for a in animals))
        org_color = {org: ORG_COLORS[i % len(ORG_COLORS)] for i, org in enumerate(orgs)}

        for animal in animals:
            loc = animal.get("location")
            if not loc:
                continue
            org = animal.get("source_org", "Unknown")
            age = animal.get("age_months", "?")
            age_str = f"{age}m" if isinstance(age, (int, float)) else str(age)
            popup_html = (
                f"<b>{animal.get('name', '')}</b><br>"
                f"{animal.get('breed', '')} · {age_str}<br>"
                f"<a href='{animal.get('source_url', '#')}' target='_blank'>{org}</a>"
            )
            folium.Marker(
                location=[loc["lat"], loc["lon"]],
                popup=folium.Popup(popup_html, max_width=230),
                tooltip=f"{animal.get('name', '')} ({org})",
                icon=folium.Icon(color=org_color[org], icon="paw", prefix="fa"),
            ).add_to(m)

        legend_parts = []
        for org, c in org_color.items():
            hex_c = ORG_HEX.get(c, "#666")
            legend_parts.append(
                f"<div><span style='color:{hex_c};font-size:16px'>●</span> {org}</div>"
            )
        legend_rows = "".join(legend_parts)
        legend_html = (
            "<div style='position:fixed;bottom:30px;right:30px;background:white;"
            "padding:8px 14px;border-radius:6px;font-size:12px;z-index:1000;"
            "border:1px solid #ccc;box-shadow:0 1px 4px rgba(0,0,0,.15)'>"
            f"{legend_rows}</div>"
        )
        m.get_root().html.add_child(folium.Element(legend_html))

    st_folium(m, use_container_width=True, height=360, key=map_key)


# ── Contact helpers ───────────────────────────────────────────────────────────

VET_WA_TEMPLATE = (
    "Hi, I have a pet emergency and found your clinic listed on Bengaluru Pet Rescue Coordinator. "
    "I need urgent veterinary assistance. Please call me back immediately. Thank you."
)
VET_EMAIL_SUBJECT = "Urgent: Pet Emergency — Veterinary Assistance Needed"
VET_EMAIL_BODY = (
    "Hi,\n\n"
    "I have a pet emergency and found your clinic listed on Bengaluru Pet Rescue Coordinator.\n\n"
    "I need urgent veterinary assistance. Please call me back as soon as possible.\n\n"
    "Thank you."
)

RESCUER_WA_TEMPLATE = (
    "Hi, I need help with an injured/stray animal rescue. "
    "I found your contact through Bengaluru Pet Rescue Coordinator. "
    "Please call me back urgently. Thank you."
)
RESCUER_EMAIL_SUBJECT = "Urgent: Animal Rescue Assistance Needed"
RESCUER_EMAIL_BODY = (
    "Hi,\n\n"
    "I need help with an injured or stray animal rescue in Bengaluru.\n\n"
    "I found your contact through Bengaluru Pet Rescue Coordinator. "
    "Please call me back as soon as possible.\n\n"
    "Thank you."
)


def _wa_link(phone: str, message: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    if not digits.startswith("91"):
        digits = "91" + digits
    return f"https://wa.me/{digits}?text={urllib.parse.quote(message)}"


def _email_link(subject: str, body: str) -> str:
    return (
        f"mailto:?subject={urllib.parse.quote(subject)}"
        f"&body={urllib.parse.quote(body)}"
    )


def _contact_buttons(phone: str, wa_template: str, email_subject: str, email_body: str) -> None:
    wa_col, email_col = st.columns(2)
    with wa_col:
        st.link_button(
            "💬 WhatsApp",
            _wa_link(phone, wa_template),
            use_container_width=True,
        )
    with email_col:
        st.link_button(
            "✉️ Email",
            _email_link(email_subject, email_body),
            use_container_width=True,
        )


# ── Cards ─────────────────────────────────────────────────────────────────────

def _animal_card(animal: dict, col) -> None:
    with col:
        with st.container(border=True):
            photo = animal.get("photo_url")
            if photo:
                st.markdown(
                    f'<img src="{photo}" '
                    f'style="width:100%;border-radius:4px;max-height:150px;object-fit:cover" '
                    f"onerror=\"this.style.display='none'\">",
                    unsafe_allow_html=True,
                )
            age = animal.get("age_months", "?")
            age_str = f"{age}m" if isinstance(age, (int, float)) else str(age)
            st.markdown(
                f"**{animal.get('name', 'Unknown')}**  \n"
                f"{animal.get('breed', '')} · {age_str} · {animal.get('sex', '')}"
            )
            tags = animal.get("temperament_tags", [])
            if tags:
                st.caption(" · ".join(str(t) for t in tags[:4]))
            org = animal.get("source_org", "")
            url = animal.get("source_url", "")
            if org and url:
                st.markdown(f"[{org}]({url})")
            elif org:
                st.caption(org)


def _vet_card(vet: dict) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        dist = vet.get("distance_km")
        dist_label = f" · {dist} km" if dist is not None else ""
        c1.markdown(f"**{vet.get('name', '')}**{dist_label}")
        c2.success("Open now")
        phone = vet.get("phone", "")
        if phone:
            st.markdown(f"📞 [{phone}](tel:{phone})")
        addr = vet.get("address", "")
        if addr:
            st.caption(addr)
        specs = vet.get("specialties", [])
        if specs:
            st.caption("Specialties: " + ", ".join(specs))
        if phone:
            _contact_buttons(phone, VET_WA_TEMPLATE, VET_EMAIL_SUBJECT, VET_EMAIL_BODY)


def _rescuer_card(rescuer: dict) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{rescuer.get('name', '')}**")
        if rescuer.get("area_match") is False:
            c2.caption("Wider BLR")
        else:
            c2.success("On-call")
        contact = rescuer.get("contact", "")
        if contact:
            st.markdown(f"📞 [{contact}](tel:{contact})")
        areas = rescuer.get("areas_covered", [])
        if areas:
            st.caption("Areas: " + ", ".join(areas[:5]))
        caps = rescuer.get("capabilities", [])
        if caps:
            st.caption("Can help with: " + ", ".join(caps))
        if contact:
            _contact_buttons(contact, RESCUER_WA_TEMPLATE, RESCUER_EMAIL_SUBJECT, RESCUER_EMAIL_BODY)


def render_cards(structured: dict) -> None:
    animals = structured.get("animals", [])
    vets = structured.get("vets", [])
    rescuers = structured.get("rescuers", [])
    protocols = structured.get("protocols", [])

    if animals:
        st.markdown("#### Available animals")
        n_cols = min(3, len(animals))
        cols = st.columns(n_cols)
        for i, a in enumerate(animals):
            _animal_card(a, cols[i % n_cols])

    if vets:
        st.markdown("#### Emergency vets nearby")
        for v in vets:
            _vet_card(v)

    if rescuers:
        st.markdown("#### On-call rescuers")
        for r in rescuers:
            _rescuer_card(r)

    if protocols:
        st.markdown("#### First-aid protocols")
        for p in protocols:
            with st.expander(p.get("title", p.get("scenario", "Protocol"))):
                st.markdown(p.get("body", ""))


# ── Emergency form dialog ─────────────────────────────────────────────────────

@st.dialog("🚨 Report an Emergency")
def show_emergency_form() -> None:
    st.markdown("Tell us what's happening and we'll find the nearest vets and rescuers right away.")

    name = st.text_input("Your name", placeholder="e.g. Rahul")

    # Seed emergency_area from picker on first open (before the widget exists in state)
    if "emergency_area" not in st.session_state:
        ul = st.session_state.user_location or {}
        saved = ul.get("place_name", "")
        st.session_state.emergency_area = "" if "Auto-detected" in saved else saved

    # Process infer request BEFORE any widget renders so the text_input picks up the new value.
    # on_click sets this flag before the dialog reruns, making the update reliable.
    if st.session_state.get("_infer_requested"):
        ul = st.session_state.user_location or {}
        if ul.get("source") == "auto":
            with st.spinner("Resolving neighbourhood…"):
                resolved = reverse_geocode(ul["lat"], ul["lon"])
            st.session_state.emergency_area = resolved or f"{ul['lat']:.4f}, {ul['lon']:.4f}"
        else:
            st.session_state.emergency_area = ul.get("place_name", "")
        del st.session_state["_infer_requested"]

    st.markdown("Your location / area in Bengaluru")
    area_col, btn_col = st.columns([4, 1])

    def _request_infer():
        st.session_state._infer_requested = True

    with btn_col:
        st.button(
            "📍 Infer",
            on_click=_request_infer,
            use_container_width=True,
            help="Auto-fill from your currently selected location",
        )

    with area_col:
        area = st.text_input(
            "Area",
            key="emergency_area",
            placeholder="e.g. Koramangala, HSR Layout, Whitefield",
            label_visibility="collapsed",
        )

    pincode = st.text_input("Pincode (optional)", placeholder="e.g. 560034")
    animal = st.selectbox(
        "Which animal needs help?",
        ["Dog", "Cat", "Bird", "Cow / Cattle", "Other"],
    )
    situation = st.text_area(
        "What happened?",
        placeholder="e.g. Found injured on the road, bleeding from the leg",
        height=100,
    )

    # Validation — all three fields must be filled before submitting
    missing = []
    if not name.strip():
        missing.append("name")
    if not area.strip():
        missing.append("location / area")
    if not situation.strip():
        missing.append("what happened")

    if missing:
        st.caption(f"Please fill in: {', '.join(missing)}")

    if st.button(
        "🚨 Get Help Now",
        type="primary",
        use_container_width=True,
        disabled=bool(missing),
    ):
        area_stripped = area.strip()
        caller = name.strip() or "anonymous"
        animal_lower = animal.lower().replace(" / ", "/")
        desc = situation.strip() or "needs immediate help"

        # Geocode typed area so the agent gets real coordinates, not just a name
        location_str = area_stripped or "Bengaluru"
        if area_stripped:
            coords = geocode_area(area_stripped)
            if coords:
                elat, elon = coords
                location_str = f"{area_stripped} (lat={elat:.4f}, lon={elon:.4f})"
                st.session_state.user_location = {
                    "lat": elat,
                    "lon": elon,
                    "place_name": area_stripped,
                    "source": "manual",
                }
        pin_stripped = pincode.strip()
        if pin_stripped:
            location_str += f", {pin_stripped}"

        # Pull verified nearby organisations from the directory
        dir_block = ""
        try:
            df_dir = load_directory()
            filtered_dir = filter_directory(df_dir, pincode=pin_stripped, area=area_stripped)
            dir_block = format_dir_for_prompt(filtered_dir, label=pin_stripped or area_stripped)
        except Exception:
            pass  # directory is supplementary — never block an emergency

        prompt = (
            f"Emergency! My name is {caller}. "
            f"I found an injured {animal_lower} near {location_str}, Bengaluru. "
            f"{desc}. What do I do right now?"
        )
        if dir_block:
            prompt += f"\n\n{dir_block}"

        # Clear emergency_area so next dialog open is fresh
        del st.session_state["emergency_area"]
        st.session_state.pending_prompt = prompt
        st.rerun()


# ── Main layout ───────────────────────────────────────────────────────────────

# Consume any pending prompt from demo buttons / emergency form
triggered = st.session_state.pending_prompt
if triggered:
    st.session_state.pending_prompt = None

# Header: health badge + title
health = fetch_health()
if health is None:
    st.error(
        "🔴 Backend unreachable — start uvicorn with: "
        "`uvicorn backend.main:app --reload --port 8000`"
    )
else:
    idx = health.get("indices", {})
    raw_n = idx.get("raw_listings") or 0
    crawler = "idle" if not raw_n else f"{raw_n} listings indexed"
    st.info(
        f"🟢 Live data: **{idx.get('animals', '?')}** animals · "
        f"**{idx.get('vets', '?')}** vets · "
        f"**{idx.get('rescuers', '?')}** rescuers · "
        f"**{idx.get('protocols', '?')}** protocols   |   Crawler: {crawler}"
    )

st.title("🐾 Bengaluru Pet Rescue & Adoption Coordinator")
st.caption("Find adoptable animals. Get emergency help fast.")

col_left, col_right = st.columns([2, 3])

# ── Left column ───────────────────────────────────────────────────────────────

with col_left:

    # ── Location picker ───────────────────────────────────────────────────────
    st.markdown("**📍 Your location**")

    # Browser geolocation — fires once per session only
    if not st.session_state.geo_attempted:
        geo = streamlit_geolocation()
        if geo is not None:
            # User responded (allow or deny) — process exactly once
            st.session_state.geo_attempted = True
            lat = geo.get("latitude")
            lon = geo.get("longitude")
            if lat is not None and lon is not None:
                in_bbox = (
                    BANGALORE_BBOX["lat"][0] <= lat <= BANGALORE_BBOX["lat"][1]
                    and BANGALORE_BBOX["lon"][0] <= lon <= BANGALORE_BBOX["lon"][1]
                )
                if in_bbox:
                    st.session_state.geo_raw = {"lat": lat, "lon": lon}
                    # Immediately update user_location so picker defaults to auto
                    st.session_state.user_location = {
                        "lat": lat,
                        "lon": lon,
                        "place_name": "📍 Auto-detected (Bengaluru)",
                        "source": "auto",
                    }
                else:
                    st.session_state.geo_out_of_bbox = True

    if st.session_state.geo_out_of_bbox:
        st.caption("We're Bengaluru-only — please search within the city.")

    # Free-text location search backed by Nominatim geocoding
    col_input, col_btn = st.columns([5, 1])
    with col_input:
        area_query = st.text_input(
            "Area",
            key="picker_query",
            placeholder="e.g. Hebbal, Yelahanka, Rajajinagar…",
            label_visibility="collapsed",
        )
    with col_btn:
        locate_clicked = st.button("🔍", use_container_width=True, help="Geocode this area")

    if locate_clicked:
        query = area_query.strip()
        if query:
            with st.spinner("Locating…"):
                result = geocode_area(query)
            if result is None:
                st.session_state.picker_status = "not_found"
            else:
                rlat, rlon = result
                in_bbox = (
                    BANGALORE_BBOX["lat"][0] <= rlat <= BANGALORE_BBOX["lat"][1]
                    and BANGALORE_BBOX["lon"][0] <= rlon <= BANGALORE_BBOX["lon"][1]
                )
                if in_bbox:
                    st.session_state.user_location = {
                        "lat": rlat,
                        "lon": rlon,
                        "place_name": query,
                        "source": "manual",
                    }
                    st.session_state.picker_status = "ok"
                else:
                    st.session_state.picker_status = "out_of_bbox"

    if st.session_state.picker_status == "not_found":
        st.warning("Area not found in Bengaluru — try a different name.", icon="⚠️")
    elif st.session_state.picker_status == "out_of_bbox":
        st.warning("That location is outside Bengaluru — we only cover the city.", icon="⚠️")

    # Location badge
    ul = st.session_state.user_location
    src_icon = "🛰️" if ul["source"] == "auto" else "📌"
    st.caption(f"{src_icon} **{ul['place_name']}** · {ul['lat']:.4f}, {ul['lon']:.4f}")

    st.divider()

    # Emergency button — full width, opens intake form
    if st.button("🚨 Emergency", type="primary", use_container_width=True):
        show_emergency_form()

    # Demo shortcut buttons
    b1, b2 = st.columns(2)
    for btn_col, (label, prompt) in zip([b1, b2], DEMO_PROMPTS):
        if btn_col.button(label, use_container_width=True):
            st.session_state.pending_prompt = prompt
            st.rerun()

    # Scrollable message history
    chat_container = st.container(height=380)
    with chat_container:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    user_typed = st.chat_input("Ask about rescue or adoption in Bengaluru…")

# ── Right column: Map + cards ─────────────────────────────────────────────────

with col_right:
    build_map(
        st.session_state.last_structured,
        user_location=st.session_state.user_location,
        map_key=f"map_{st.session_state.response_count}",
    )
    render_cards(st.session_state.last_structured)

# ── Process input ─────────────────────────────────────────────────────────────

user_input = triggered or user_typed

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.spinner("Consulting the agent…"):
        reply, structured = call_chat(user_input)

    if reply is None:
        reply = "Backend unreachable — check that uvicorn is running on port 8000."

    st.session_state.messages.append({"role": "assistant", "content": reply})
    _save_to_history(st.session_state.session_id, st.session_state.messages)

    if structured:
        st.session_state.last_structured = structured
    st.session_state.response_count += 1

    st.rerun()
