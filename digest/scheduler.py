from datetime import datetime, timedelta, timezone


def next_run(after: datetime, hour_utc: int) -> datetime:
    candidate = after.astimezone(timezone.utc).replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= after.astimezone(timezone.utc):
        candidate += timedelta(days=1)
    return candidate
