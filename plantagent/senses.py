"""What the plant can sense. Phase 1: only the clock.

Later you can add real sensors here (soil moisture, light, temperature) and
the plant will 'feel' them automatically, because this text goes into its
prompt every turn. Example for a Raspberry Pi is in the README.
"""
from __future__ import annotations

from datetime import datetime


def get_senses() -> dict:
    now = datetime.now()
    hour = now.hour
    part = "night" if hour < 6 or hour >= 22 else "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    season = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
              6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}[now.month]
    return {
        "time": now.strftime("%A %d %B %Y, %H:%M"),
        "part_of_day": part,
        "season_northern_hemisphere": season,
        # Not connected yet. Fill these in when you add sensors:
        "soil_moisture": None,
        "light": None,
        "temperature": None,
    }


def describe_senses() -> str:
    s = get_senses()
    lines = [f"It is {s['time']} ({s['part_of_day']}, {s['season_northern_hemisphere']})."]
    missing = [k for k in ("soil_moisture", "light", "temperature") if s[k] is None]
    if missing:
        lines.append("You have no sensors yet (" + ", ".join(missing) + "), so you cannot feel those.")
    for k in ("soil_moisture", "light", "temperature"):
        if s[k] is not None:
            lines.append(f"{k.replace('_', ' ').title()}: {s[k]}")
    return "\n".join(lines)
