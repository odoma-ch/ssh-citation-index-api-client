"""Jobs: everything submitted this session, plus lookup of any job by ID."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import core

st.title("Jobs")
st.caption(
    "Jobs keep running on the server after you leave a page or close the browser. "
    "Look one up by ID to see its status and fetch its result."
)



def live_state(job: dict) -> str:
    """Cached outcome, or the server's current status for jobs still in flight.

    A page only polls while it is open, so a job whose page was left keeps
    running on the server with no cached result — ask the server instead.
    """
    if "error" in job:
        return "failed"
    if "result" in job:
        return "completed"
    if not job.get("job_id"):
        return "unknown"
    try:
        status = core.client().get_job_status(job["job_id"])
    except Exception:  # noqa: BLE001 — a lookup failure shouldn't break the table
        return "unknown"
    stage = f" · {status.current_stage}" if status.current_stage and not status.is_terminal else ""
    return f"{status.status}{stage}"


history = core.jobs()
picked: list[int] = []
if history:
    rows = [
        {
            "submitted": datetime.fromtimestamp(job["submitted"]).strftime("%H:%M:%S"),
            "step": job["label"],
            "job ID": job.get("job_id") or "",
            "state": live_state(job),
        }
        for job in sorted(history.values(), key=lambda j: j["submitted"], reverse=True)
    ]
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()
    st.caption(
        "Select a row to look the job up. A finished job's results show on its page "
        "when you go back to it."
    )
    picked = st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row"
    ).selection.rows
    selected = rows[picked[0]]["job ID"] if picked else None
    # Prefill only when the selection changes, so a typed ID isn't overwritten.
    if selected and selected != st.session_state.get("job-picked"):
        st.session_state["job-id"] = selected
    st.session_state["job-picked"] = selected
    picked = picked if selected and st.session_state["job-id"] == selected else []
else:
    st.caption("No jobs submitted in this session yet.")

with st.form("job-lookup"):
    left, right = st.columns([4, 1])
    job_id = left.text_input("Job ID", key="job-id")
    fmt = right.selectbox("Result format", ["json", "xml"])
    lookup = st.form_submit_button("Look up", type="primary")

if (lookup or picked) and job_id.strip():
    job_id = job_id.strip()
    client = core.client()
    try:
        status = client.get_job_status(job_id)
    except Exception as exc:  # noqa: BLE001
        st.error(core.describe(exc))
        st.stop()
    stage = f" · {status.current_stage}" if status.current_stage else ""
    show = {"completed": st.success, "failed": st.error}.get(status.status, st.info)
    show(f"**{status.status}**{stage}")
    if status.error:
        st.error(status.error)
    with st.expander("Status details"):
        st.json(vars(status))
    if status.status == "completed":
        try:
            result = client.get_job_result(job_id, format=fmt)
        except Exception as exc:  # noqa: BLE001
            st.error(core.describe(exc))
            st.stop()
        if fmt == "xml":
            st.download_button("Download", result, f"{job_id}.xml", on_click="ignore")
            st.code(result[:200_000], language="xml")
        else:
            core.json_download(result, job_id, key="lookup-json")
            st.json(result, expanded=2)
