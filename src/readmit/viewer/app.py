"""Case timeline: one patient's whole story on one screen.

    make timeline         # http://localhost:8502

Read only. Pick a case (or a shortcut to an interesting one) and read it top to bottom: discharge and risk, every
message (patient, AI, nurse), what the AI decided and why, nurse work, visits, re-scores and the outcome.
"""

import streamlit as st

from readmit.nurse import console
from readmit.outreach import store
from readmit.viewer import timeline

st.set_page_config(page_title="Case timeline", layout="wide")
st.title("Case timeline")
st.caption(f"Simulation clock: {store.now():%a %b %d, %H:%M} · read only · synthetic patients")

all_cases = timeline.cases()
if not all_cases:
    st.info("No outreach cases yet. Run `make outreach-setup` and `make simulate days=30` first.")
    st.stop()

ids = [c["case_id"] for c in all_cases]
by_id = {c["case_id"]: c for c in all_cases}
if "case_id" not in st.session_state or st.session_state.case_id not in by_id:
    st.session_state.case_id = ids[0]

with st.sidebar:
    st.markdown("#### Interesting cases")
    for x in timeline.interesting():
        if st.button(f"{x['label']} · {x['case_id']}", key=f"short-{x['label']}"):
            st.session_state.case_id = x["case_id"]
    st.markdown("#### Any case")
    st.selectbox(
        "Case",
        ids,
        key="case_id",
        format_func=lambda i: (
            f"{i} · {by_id[i]['first_name']} · {by_id[i]['tier']} · {by_id[i]['status']} · {by_id[i]['messages']} msgs"
        ),
    )
    st.markdown("#### Show")
    show = {k: st.checkbox(k, value=k != "status") for k in timeline.KINDS}
    translate = st.checkbox("Translate non-English messages (LLM, cached)", value=False)

t = timeline.build(st.session_state.case_id)
h = t["header"]

st.subheader(f"{h['first_name']}, {h['age']} · {h['diagnosis']} · case {h['case_id']}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Risk at discharge", f"{h['risk_at_discharge'] * 100:.0f}%" if h["risk_at_discharge"] is not None else "-")
c2.metric("Risk now", f"{h['risk_now'] * 100:.0f}%")
c3.metric("Tier", h["tier"] or "-")
c4.metric("Status", h["status"])
c5.metric("Attempts", h["attempts"])
st.write(
    f"**Discharged** {h['discharged']} · **Follow up by** {h['follow_up_by'] or '-'} with {h['own_doctor'] or '-'}"
    f" · **Language** {h['language']}{' (patient asked)' if h['language_source'] == 'patient' else ''}"
    f" · **Channels** {', '.join(h['channels']) or 'none'} · **Video** {'yes' if h['telehealth_ok'] else 'no'}"
)
st.write(
    f"**Why flagged** {', '.join(h['drivers']) or '-'} · **Barriers** "
    f"{', '.join(b.replace('_', ' ') for b in h['barriers']) or 'none'}"
)
if len(t["risk"]) > 1:
    st.line_chart({"day": [d for d, _ in t["risk"]], "risk": [r for _, r in t["risk"]]}, x="day", y="risk", height=180)

st.markdown("#### What happened")
day = None
for i in t["items"]:
    if not show.get(i["kind"], True):
        continue
    d = int(i["day"]) if i["day"] is not None else None
    if d != day:
        day = d
        st.markdown(f"##### Day {d} · {i['at'][:10]}" if d is not None else f"##### {i['at'][:10]}")
    if i["kind"] == "message" and i.get("body"):
        role = "user" if i["actor"] == "patient" else "assistant"
        avatar = {"patient": "🧑", "nurse": "🩺"}.get(i["actor"], "🤖")
        with st.chat_message(role, avatar=avatar):
            st.caption(
                f"{i['at'][11:16]} · {i['title']} · {i['channel']} · {i['status']}"
                + (f" · {i['detail']}" if i["detail"] else "")
            )
            if i.get("subject"):
                st.markdown(f"**{i['subject']}**")
            st.write(i["body"])
            if translate and console.needs_translation(i["body"]):
                tr = console.translate(i["body"])
                st.caption(
                    f"Machine translation from {tr['language']}: {tr['english']}" if tr else "Translation unavailable."
                )
    else:
        line = f"{timeline.ICONS[i['kind']]} `{i['at'][11:16]}` {i['title']}" + (
            f" · {i['detail']}" if i["detail"] else ""
        )
        if i["kind"] == "nurse" and i.get("priority") == "urgent":
            st.warning(line)
        elif i["kind"] == "outcome":
            st.info(line)
        else:
            st.markdown(line)

with st.expander("Counts"):
    st.write(t["counts"])
