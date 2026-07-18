"""geohazard-chat frontend — M0 stub.

M0 scope: prove the frontend container boots and reaches the backend.
The real map/chat UI (SS5.1) lands in M1.
"""
import os

import requests
import streamlit as st

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000")

st.set_page_config(page_title="Geohazard Chat", page_icon=":earth_africa:")
st.title("Geohazard Chat — M0 scaffolding")

st.info(
    "This is an automated first-look analysis of public satellite data. "
    "It is not a safety assessment or an official hazard evaluation."
)

st.subheader("Stack health")
try:
    r = requests.get(f"{BACKEND_URL}/health", timeout=5)
    data = r.json()
    if data.get("healthy"):
        st.success("Backend, database and broker are all reachable.")
    else:
        st.warning("Stack is up but degraded:")
    st.json(data)
except Exception as e:
    st.error(f"Backend unreachable at {BACKEND_URL}: {e}")

st.caption(
    "M1 adds: map AOI selection, chat input, depth selector, progress polling. "
    "Powered by free Copernicus / COMET-LiCSAR / EGMS / ASF data (attribution "
    "footer assembled per SS10 once real results exist)."
)
