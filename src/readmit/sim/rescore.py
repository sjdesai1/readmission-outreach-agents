"""Live re-scoring during the simulation: the case's risk is updated from the discharge risk (the prior) plus what
was learned since: pharmacy fills and ED visits from the world's signals, follow-up visits from the outreach system.

  events      re-score right away when something happens, and alert a nurse if the risk jumps:
                ED visit (sent home)          urgent callback: often the warning sign days before a readmission
                no prescription fill by day 3 routine callback
  landmarks   day 7, 14 and 21: a safety net for everything that changes quietly
Cases whose outreach ended (visit done, unreachable) are still re-scored and alerted: an ED visit after a completed
follow-up visit still deserves a call. The callback doesn't reopen outreach (the store only pauses open cases).
Patients who opted out get no call. An event before the case opens (discharge + 24h) is handled when it opens.

A missed fill re-scores but rarely alerts: by the data, it barely moves the risk (odds ratio about 1.2).

Skipped if `make landmark` hasn't saved the update models.
"""

from datetime import timedelta

import pandas as pd

from readmit import db
from readmit.model import landmark
from readmit.outreach import store

FILL_BY_DAY = 3.0
SEEN_AFTER = 1 / 24  # an event is known an hour after it happens
ALERT_RISE = 0.05  # alert a nurse when an event raises the risk by 5 points or more ...
ALERT_RISK = 0.50  # ... or leaves it at 50% or higher
PRIORITY = {"ed_visit": "urgent", "missed_fill": "routine"}
WHY = {
    "ed_visit": "ED visit after discharge (sent home)",
    "missed_fill": f"no discharge prescription filled by day {FILL_BY_DAY:g}",
}


def triggers_for(events: pd.DataFrame) -> list[tuple[float, str]]:
    """Event re-scores for one stay, as (day after discharge, trigger), in time order."""
    days = pd.to_numeric(events["day"], errors="coerce")
    out = [(float(d) + SEEN_AFTER, "ed_visit") for d in days[events["type"] == "ed_visit"]]
    if not (days[events["type"] == "rx_fill"] < FILL_BY_DAY).any():
        out.append((FILL_BY_DAY, "missed_fill"))
    return sorted(out)


def alert_priority(trigger: str, before: float, after: float) -> str | None:
    """Should a nurse call, and how soon. Landmarks never alert: they only reorder outreach."""
    if trigger == "landmark" or (after - before < ALERT_RISE and after < ALERT_RISK):
        return None
    return PRIORITY[trigger]


class Rescorer:
    def __init__(self):
        self.models = landmark.load_models()
        self.alerts = 0  # nurse callbacks created so far
        self.alert_errors: list[str] = []  # callbacks the store refused, with its reason
        self.pending: list[tuple] = []  # (due time, case_id, encounter_id, day, trigger, discharge)
        with db.transaction() as c:
            have_tables = all(
                db.one(c, f"select to_regclass('{t}') is not null as ok")["ok"]
                for t in ("cohort.stays", "scoring.scores", "sim.post_discharge")
            )
        if not self.models or not have_tables:
            self.models = {}
            return
        with db.transaction() as c:
            rows = db.rows(
                c,
                """select k.case_id, k.encounter_id, k.opened_at, s.discharge_datetime, sc.risk as prior
                   from outreach.cases k
                   join cohort.stays s using (encounter_id)
                   join scoring.scores sc using (encounter_id)""",
            )
        self.prior = {r["encounter_id"]: float(r["prior"]) for r in rows}
        world = db.read(
            "select encounter_id, day, type from sim.post_discharge where encounter_id = any(:ids)",
            ids=list(self.prior),
        )
        for r in rows:
            mine = world[world["encounter_id"] == r["encounter_id"]]
            safety_net = [(float(L), "landmark") for L in landmark.LANDMARKS if L in self.models]
            schedule = safety_net + triggers_for(mine)
            for day, trigger in schedule:
                due = max(r["discharge_datetime"] + timedelta(days=day), r["opened_at"])
                seen = (due - r["discharge_datetime"]).total_seconds() / 86400  # what is known by then
                self.pending.append((due, r["case_id"], r["encounter_id"], seen, trigger, r["discharge_datetime"]))
        self.pending.sort()

    def run(self, t) -> int:
        """Re-score every case whose landmark or event has come. Returns how many were re-scored."""
        n = 0
        while self.pending and self.pending[0][0] <= t:
            _, case_id, enc, day, trigger, discharge = self.pending.pop(0)
            events = self._events(case_id, enc, discharge)
            risk = landmark.live_score(landmark.nearest(self.models, day), enc, self.prior[enc], events, day)
            r = store.rescore(case_id, risk, day, trigger)
            if "skipped" in r:
                continue
            n += 1
            priority = alert_priority(trigger, r["before"], r["after"]) if r["status"] != "opted_out" else None
            if priority:
                error = self._alert(case_id, trigger, priority, r, day)
                if error:
                    self.alert_errors.append(f"{case_id} {trigger}: {error}")
                else:
                    self.alerts += 1
        return n

    @staticmethod
    def _alert(case_id: str, trigger: str, priority: str, r: dict, day: float) -> str | None:
        reason = f"{WHY[trigger]}; risk {r['before']:.0%} -> {r['after']:.0%}. Call to check in."
        try:
            store.create_nurse_task(case_id, "callback", priority, reason, f"{case_id}:rescore:{trigger}:{day:.2f}")
            return None
        except store.StoreError as e:
            return str(e)  # e.g. the case isn't open yet: the re-score still counts

    @staticmethod
    def _events(case_id: str, enc: str, discharge) -> pd.DataFrame:
        """World signals for this stay + follow-up visits the outreach actually got the patient to."""
        with db.transaction() as c:
            world = db.rows(c, "select encounter_id, day, type from sim.post_discharge where encounter_id = :e", e=enc)
            visits = db.rows(
                c,
                "select start_at from outreach.appointments where case_id = :id and status = 'completed'",
                id=case_id,
            )
        kept = [
            {"encounter_id": enc, "day": (v["start_at"] - discharge).total_seconds() / 86400, "type": "followup_kept"}
            for v in visits
        ]
        return pd.DataFrame(world + kept, columns=["encounter_id", "day", "type"])
