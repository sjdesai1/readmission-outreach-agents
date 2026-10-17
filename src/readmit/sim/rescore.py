"""Live landmark re-scoring during the simulation: on day 7, 14 and 21 after each discharge, the case's risk is
recomputed from the discharge features plus what was learned since (pharmacy fills, ED visits from the world's
signals; follow-up visits from the outreach system). Skipped if `make landmark` hasn't saved models.
"""

from datetime import timedelta

import pandas as pd

from readmit import db
from readmit.model import landmark
from readmit.outreach import store


class Rescorer:
    def __init__(self):
        self.models = landmark.load_models()
        self.pending: list[tuple] = []  # (due time, case_id, encounter_id, landmark day, discharge)
        with db.transaction() as c:
            have_tables = all(
                db.one(c, f"select to_regclass('{t}') is not null as ok")["ok"]
                for t in ("cohort.stays", "features.social_extracted", "sim.post_discharge")
            )
        if not self.models or not have_tables:
            self.models = {}
            return
        with db.transaction() as c:
            rows = db.rows(
                c,
                """select k.case_id, k.encounter_id, s.discharge_datetime from outreach.cases k
                   join cohort.stays s using (encounter_id)""",
            )
        for r in rows:
            for L in self.models:
                self.pending.append(
                    (
                        r["discharge_datetime"] + timedelta(days=L),
                        r["case_id"],
                        r["encounter_id"],
                        L,
                        r["discharge_datetime"],
                    )
                )
        self.pending.sort()
        ids = [r["encounter_id"] for r in rows]
        self.stays = db.read("select * from cohort.stays where encounter_id = any(:ids)", ids=ids)
        self.social = db.read("select * from features.social_extracted where encounter_id = any(:ids)", ids=ids)

    def run(self, t) -> int:
        """Re-score every case whose landmark has come. Returns how many were re-scored."""
        n = 0
        while self.pending and self.pending[0][0] <= t:
            _, case_id, enc, L, discharge = self.pending.pop(0)
            events = self._events(case_id, enc, discharge)
            stay = self.stays[self.stays["encounter_id"] == enc]
            if stay.empty:
                continue
            risk = landmark.live_score(self.models[L], stay, self.social, events, L)
            if "skipped" not in store.rescore(case_id, risk, L):
                n += 1
        return n

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
