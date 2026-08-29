"""Activity ingestion: parse FIT files into a validated per-second record frame."""

from pace_predict.io.fit import Activity, ActivityMeta, load_activity, running_sessions

__all__ = ["Activity", "ActivityMeta", "load_activity", "running_sessions"]
