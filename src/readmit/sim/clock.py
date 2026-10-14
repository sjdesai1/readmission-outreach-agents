"""The simulation's calendar. Every module reads dates from here, so they always agree."""

from datetime import datetime

WARMUP_START = datetime(2023, 9, 1)  # history starts here so "prior admissions" counts are real; not exported
EXPORT_START = datetime(2024, 9, 1)  # first stay visible in the data
TRAIN_END = datetime(2026, 3, 1)  # train = discharged before this
TEST_END = datetime(2026, 8, 1)  # test = discharged TRAIN_END..TEST_END (labels mature by LIVE_START)
LIVE_START = datetime(2026, 9, 1)  # "go-live": model frozen, outreach begins
LIVE_END = datetime(2026, 11, 30, 23, 59)  # end of the simulated outreach period

READMIT_WINDOW_DAYS = 30
PREDICTION_LAG_HOURS = 24  # patients are scored 24h after discharge
