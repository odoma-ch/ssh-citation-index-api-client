"""Streamlit front-end for the Citation Index API.

Run with ``streamlit run app/streamlit_app.py``.
"""

from __future__ import annotations

import hmac
import os

import streamlit as st

import core
from citation_index_api_client import APIError
from citation_index_api_client.client import API_KEY_ENV

st.set_page_config(page_title="Citation Index", page_icon="📚", layout="wide")

MIN_STREAMLIT = (1, 66)
if tuple(int(part) for part in st.__version__.split(".")[:2]) < MIN_STREAMLIT:
    st.error(
        f"This app needs Streamlit {'.'.join(map(str, MIN_STREAMLIT))} or newer; this environment has "
        f"{st.__version__}. Run `pip install -e \".[app]\"` here, or start it with `.venv/bin/streamlit`."
    )
    st.stop()


def require_login() -> None:
    """Optional shared login, set with CITATION_INDEX_UI_AUTH=user:password."""
    credentials = os.getenv("CITATION_INDEX_UI_AUTH")
    if not credentials or st.session_state.get("authenticated"):
        return
    expected_user, _, expected_password = credentials.partition(":")
    with st.form("login"):
        st.subheader("Sign in")
        user = st.text_input("User")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            ok = hmac.compare_digest(user, expected_user) & hmac.compare_digest(
                password, expected_password
            )
            if ok:
                st.session_state["authenticated"] = True
                st.rerun()
            st.error("Wrong user or password.")
    st.stop()


@st.cache_data(ttl=60, show_spinner=False)
def connection_state(api_key: str | None) -> tuple[str, str]:
    """(level, message) describing whether the API is reachable and the key works."""
    client = core.CitationIndexClient(core.BASE_URL, api_key=api_key, timeout=10)
    try:
        version = client.health().get("version", "?")
        # /health is open but / needs the key, so it doubles as the key check.
        try:
            info = client.root()
        except APIError as exc:
            if exc.status_code == 401:
                return "error", f"API {version} reachable, but the key is missing or was rejected."
            raise
        docs = f"{core.BASE_URL}{info.get('docs', '/docs')}"
        return "success", f"Connected · {info.get('name', 'API')} {version} · [docs]({docs})"
    except Exception as exc:  # noqa: BLE001 — anything here means "not connected"
        return "error", f"Cannot reach the API: {core.describe(exc)}"
    finally:
        client.close()


def sidebar() -> None:
    with st.sidebar:
        st.markdown("### Connection")
        st.caption(f"API: `{core.BASE_URL}`")
        env_key = bool(os.getenv(API_KEY_ENV))
        st.text_input(
            "API key",
            type="password",
            key="api_key",
            placeholder=f"Using {API_KEY_ENV}" if env_key else "Required",
            help="Sent as the X-API-Key header. Kept in this browser session only.",
        )
        level, message = connection_state(core.api_key())
        getattr(st, level)(message)


require_login()
sidebar()
st.navigation(
    {
        "Workflows": [
            st.Page("views/pipeline.py", title="End-to-end pipeline", icon="🔁", default=True),
        ],
        "Single steps": [
            st.Page("views/extract.py", title="Extract", icon="📄"),
            st.Page("views/parse.py", title="Parse", icon="🧩"),
            st.Page("views/link.py", title="Link", icon="🔗"),
            st.Page("views/classify.py", title="Classify", icon="🏷️"),
        ],
        "Jobs": [st.Page("views/jobs.py", title="Jobs", icon="🗂️")],
    }
).run()
