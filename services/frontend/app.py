"""geohazard-chat frontend — M1.2 (§5.1: map + chat + progress + results).

Flow: draw AOI on the folium map -> type a question in the chat box ->
POST /query -> poll GET /status every 3 s (st.fragment) -> on done/failed
fetch GET /result and render the answer + artifact PNGs.

Streamlit-specific choices (§4.1): polling, not WebSockets; images are
fetched server-side from the backend so the browser never needs direct
backend access.
"""
from __future__ import annotations

import os
import time

import folium
import requests
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000")
POLL_SECONDS = 3

DISCLAIMER = (
    "This is an automated first-look analysis of public satellite data. "
    "It is not a safety assessment or an official hazard evaluation."
)

st.set_page_config(page_title="Geohazard Chat", page_icon="🌍", layout="wide")
st.title("🌍 Geohazard Chat")
st.info(DISCLAIMER, icon="⚠️")

ss = st.session_state
ss.setdefault("query_id", None)      # in-flight query
ss.setdefault("history", [])         # [{question, answer, images:[(bytes,caption)], status}]
ss.setdefault("aoi", None)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Analysis settings")
    depth = st.radio(
        "Depth",
        options=["quick", "standard", "thorough"],
        index=1,
        help="Quick: 1 method · Standard: 2 · Thorough: all applicable (§5.1)",
    )
    use_dates = st.checkbox("Limit date range", value=False)
    dates = {"start": None, "end": None}
    if use_dates:
        c1, c2 = st.columns(2)
        dates["start"] = str(c1.date_input("From"))
        dates["end"] = str(c2.date_input("To"))
    st.divider()
    try:
        h = requests.get(f"{BACKEND_URL}/health", timeout=4).json()
        ok = h.get("healthy")
        st.caption(("🟢" if ok else "🟡") + " backend " + ("healthy" if ok else "degraded"))
        llm_state = h.get("services", {}).get("llm", "?")
        st.caption(("🟢" if llm_state == "ok" else "🔴") + f" LLM: {llm_state[:60]}")
    except Exception:
        st.caption("🔴 backend unreachable")

# ---------------------------------------------------------------- map
left, right = st.columns([3, 2], gap="large")

with left:
    st.subheader("1 · Draw your area")
    m = folium.Map(location=[40.18, 44.51], zoom_start=10, tiles="OpenStreetMap")
    Draw(
        draw_options={
            "polygon": True, "rectangle": True,
            "circle": False, "marker": False, "polyline": False, "circlemarker": False,
        },
        edit_options={"edit": False},
    ).add_to(m)
    map_out = st_folium(m, height=430, use_container_width=True, key="aoi_map")

    drawing = (map_out or {}).get("last_active_drawing")
    if drawing and drawing.get("geometry", {}).get("type") == "Polygon":
        ss.aoi = drawing["geometry"]
    if ss.aoi:
        n = len(ss.aoi["coordinates"][0])
        st.caption(f"AOI captured: polygon with {n} vertices. Draw again to replace.")
    else:
        st.caption("No AOI yet — use the ▭ or ⬠ tool on the map.")

# ---------------------------------------------------------------- status polling
def _render_history(container):
    for item in ss.history:
        with container.chat_message("user"):
            st.write(item["question"])
        with container.chat_message("assistant"):
            if item["status"] == "failed":
                st.error("Analysis failed — details below.")
            elif item["status"] == "needs_clarification":
                st.warning("Clarification needed — please rephrase your question.")
            st.write(item["answer"])
            for img_bytes, caption in item.get("images", []):
                st.image(img_bytes, caption=caption)


def _fetch_final(query_id: str) -> dict:
    r = requests.get(f"{BACKEND_URL}/result/{query_id}", timeout=10).json()
    images = []
    for a in r.get("artifacts", []):
        try:
            img = requests.get(f"{BACKEND_URL}{a['url']}", timeout=10)
            img.raise_for_status()
            images.append((img.content, a.get("caption", "")))
        except Exception:
            pass
    return {"answer": r.get("answer") or "(no answer)", "images": images,
            "status": r.get("status", "done")}


with right:
    st.subheader("2 · Ask")
    chat_box = st.container()
    _render_history(chat_box)

    if ss.query_id:
        @st.fragment(run_every=f"{POLL_SECONDS}s")
        def poll_status():
            qid = ss.query_id
            if not qid:
                return
            try:
                s = requests.get(f"{BACKEND_URL}/status/{qid}", timeout=10).json()
            except Exception as e:
                st.warning(f"status poll failed: {e}")
                return
            status = s.get("status")
            if status in ("done", "failed", "needs_clarification"):
                final = _fetch_final(qid)
                ss.history[-1].update(final)
                ss.query_id = None
                st.rerun()  # full rerun: render answer into history, stop polling
            else:
                prog = s.get("progress", [])
                pct = prog[-1]["percent"] if prog else 0
                msg = prog[-1]["message"] if prog else "queued…"
                st.progress(pct / 100.0, text=f"{status} — {msg} ({pct}%)")
                with st.expander("progress log", expanded=False):
                    for p in prog[-10:]:
                        st.caption(f"{p['ts'][11:19]} · {p['percent']}% · {p['message']}")

        poll_status()

    question = st.chat_input(
        "e.g. is the ground moving here? / здесь есть проседание грунта?",
        disabled=ss.query_id is not None,
    )
    if question:
        if not ss.aoi:
            st.error("Draw an area on the map first.")
        else:
            payload = {
                "question": question,
                "aoi": ss.aoi,
                "dates": dates,
                "depth": depth,
                "expert_raw": False,
            }
            try:
                r = requests.post(f"{BACKEND_URL}/query", json=payload, timeout=15)
                if r.status_code == 202:
                    ss.query_id = r.json()["query_id"]
                    ss.history.append(
                        {"question": question, "answer": "", "images": [], "status": "running"}
                    )
                    st.rerun()
                else:
                    detail = r.json().get("detail", r.text)
                    st.error(f"Rejected ({r.status_code}): {detail}")
            except Exception as e:
                st.error(f"Backend unreachable: {e}")

st.divider()
st.caption(
    DISCLAIMER + "  ·  Powered by free public satellite data "
    "(Copernicus Sentinel · EGMS © EU CLMS · COMET-LiCSAR · ASF HyP3) — "
    "full per-answer attribution appears with real results from M2."
)
