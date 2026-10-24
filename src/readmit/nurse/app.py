"""Nurse queue screen.

    make nurse            # http://localhost:8501

Left: the queue (emergencies first, then urgent, then by due time). Right: the patient, the shared thread
(patient, AI and nurse messages), a reply box and resolve / hand back. Reads and writes through the same store
rules as the agents: consent, quiet hours, idempotency.
"""

import streamlit as st

from readmit.nurse import console
from readmit.outreach import store

st.set_page_config(page_title="Nurse queue", layout="wide")
st.title("Nurse queue")
st.caption(f"Simulation clock: {store.now():%a %b %d, %H:%M}")

cap = console.capacity()
c1, c2, c3 = st.columns(3)
c1.metric("Urgent open", cap["urgent_open"])
c2.metric("Routine open", sum(cap["routine_by_day"].values()))
c3.metric("Routine capacity / day", cap["daily_capacity"])

tasks = console.queue()
if not tasks:
    st.success("Queue is empty.")
    st.stop()

left, right = st.columns([1, 2])
with left:
    labels = [
        f"{'🚨 ' if t['kind'] == 'emergency' else '⚠️ ' if t['priority'] == 'urgent' else ''}"
        f"{t['patient']} · {t['kind']} · due {t['due_at'][5:16]}"
        + (f" · interpreter: {t['interpreter']}" if t.get("interpreter") else "")
        + (f" · {t['minutes_late']} min late" if t["minutes_late"] else "")
        for t in tasks
    ]
    pick = st.radio("Open tasks", range(len(tasks)), format_func=lambda i: labels[i])
task = tasks[pick]
view = console.case_view(task["case_id"])
s = view["summary"]

with right:
    st.subheader(f"{s['patient']['first_name']} · {s['patient']['preferred_language']} · case {task['case_id']}")
    st.write(f"**Task:** {task['kind']} ({task['priority']}) · {task['reason']}")
    if task.get("interpreter"):
        st.warning(f"Interpreter needed: {task['interpreter']}. Book one before calling; don't reply in English.")
    if s["patient"].get("language_source") == "patient":
        st.caption(f"Language: {s['patient']['preferred_language']} (the patient asked for it)")
    risk = s.get("risk") or {}
    st.write(
        f"**Risk:** {risk.get('tier')} ({risk.get('risk')}) · **Barriers:** {', '.join(risk.get('barriers') or []) or 'none'}"
        f" · **Stay:** {(s.get('stay') or {}).get('diagnosis_group')}"
    )
    appt = view["appointment"]
    st.write(f"**Visit:** {appt['start_at'][:16] + ' with ' + appt['provider'] if appt else 'none booked'}")

    st.markdown("#### Thread")
    who = {"patient": ("user", "Patient"), "ai": ("assistant", "AI"), "nurse": ("assistant", "Nurse")}
    for m in view["thread"]:
        role, label = who.get(m["author"], ("assistant", m["author"]))
        with st.chat_message(role, avatar="🧑" if role == "user" else ("🩺" if m["author"] == "nurse" else "🤖")):
            st.caption(f"{label} · {m['channel']} · {m['at'][:16]} · {m['status']}")
            st.write(m["body"])
            if console.needs_translation(m["body"]):
                t = console.translate(m["body"])
                st.caption(
                    f"Machine translation from {t['language']}, check with an interpreter before acting: {t['english']}"
                    if t
                    else "Machine translation unavailable."
                )

    allowed = s["contact"]["allowed_channels"]
    with st.form("reply", clear_on_submit=True):
        st.markdown("#### Reply to the patient")
        channel = st.selectbox("Channel", allowed or ["(no consented channel: call from the phone on file)"])
        body = st.text_area("Message")
        if st.form_submit_button("Send") and body.strip() and allowed:
            try:
                console.reply(task["task_id"], task["case_id"], channel, body, urgent=task["priority"] == "urgent")
                st.success("Sent")
            except store.StoreError as e:
                st.error(str(e))

    with st.form("resolve"):
        st.markdown("#### Resolve")
        note = st.text_input("What you did")
        hand_back = st.checkbox("Hand back to the AI outreach", value=True)
        if st.form_submit_button("Resolve task") and note.strip():
            console.resolve(task["task_id"], note, hand_back)
            st.success("Resolved")
            st.rerun()
