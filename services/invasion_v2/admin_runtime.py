"""Process-local observations for the admin health screen; never a DB flag."""
from datetime import datetime

last_successful_tick: datetime | None = None
last_error: str | None = None
