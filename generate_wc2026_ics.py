"""
Generate a subscribable .ics calendar for the 2026 FIFA World Cup.

Tiers (richness of event):
  - Tier 1 (England)      : Full briefing description + 60min and 15min VALARM reminders
  - Tier 2 (marquee/rival): Full briefing description, no alarms
  - Tier 3 (other)        : Minimal description

Reads:  wc2026_schedule.yaml  (in same directory)
Writes: world_cup_2026.ics    (in same directory)

Refreshes: clients honour REFRESH-INTERVAL of PT1H so subscribed feeds re-fetch hourly.
"""

from __future__ import annotations

import hashlib
import re
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml  # PyYAML

# --------------------------- Configuration ---------------------------

HERE = Path(__file__).parent
SCHEDULE_PATH = HERE / "wc2026_schedule.yaml"
OUTPUT_PATH = HERE / "world_cup_2026.ics"

# Bump this whenever the calendar is regenerated — clients will see updated events.
GENERATED_AT = datetime.now(timezone.utc)

# Calendar metadata
CAL_NAME = "World Cup 2026"
CAL_DESC = "All 104 matches of the 2026 FIFA World Cup. England matches include broadcaster, stadium, lineup link and pre-match reminders. Curated by Oliver."
CAL_TIMEZONE = "Europe/London"
PRODID = "-//Oliver Wakelin//World Cup 2026//EN"

# Each event lasts this many minutes (90 min match + ~15 min buffer for stoppage)
DEFAULT_DURATION_MINUTES = 105
KNOCKOUT_DURATION_MINUTES = 135  # allow for extra time + penalties

# Tier 1 — events get reminders
ENGLAND_TEAM = "England"

# Tier 2 — marquee / rival nations
MARQUEE_TEAMS = {
    # Top-tier rivals
    "France", "Germany", "Brazil", "Argentina", "Spain", "Portugal", "Netherlands",
    # Hosts
    "United States", "Canada", "Mexico",
    # Home nation
    "Scotland",
    # Other strong sides worth marking
    "Italy", "Belgium", "Uruguay",
}

# Group stage matches that ITV will broadcast (from ITV press release, 10 Dec 2025).
# Keyed by (date, home, away) where the names match the schedule YAML exactly.
# Anything not in this set during the group stage defaults to BBC.
ITV_GROUP_MATCHES = {
    ("2026-06-11", "Mexico", "South Africa"),
    ("2026-06-12", "South Korea", "Winner Play-off D"),
    ("2026-06-13", "Qatar", "Switzerland"),
    ("2026-06-14", "Australia", "Winner Play-off C"),
    ("2026-06-14", "Germany", "Curacao"),
    ("2026-06-14", "Netherlands", "Japan"),
    ("2026-06-15", "Winner Play-off B", "Tunisia"),
    ("2026-06-15", "Spain", "Cape Verde"),
    ("2026-06-15", "Saudi Arabia", "Uruguay"),
    ("2026-06-17", "Argentina", "Algeria"),
    ("2026-06-17", "England", "Croatia"),
    ("2026-06-18", "Ghana", "Panama"),
    ("2026-06-18", "Winner Play-off A", "Switzerland"),
    ("2026-06-18", "Canada", "Qatar"),
    ("2026-06-19", "Scotland", "Morocco"),
    ("2026-06-20", "Paraguay", "Winner Play-off C"),
    ("2026-06-20", "Brazil", "Haiti"),
    ("2026-06-20", "Germany", "Ivory Coast"),
    ("2026-06-21", "Belgium", "Iran"),
    ("2026-06-22", "Egypt", "New Zealand"),
    ("2026-06-23", "Senegal", "Norway"),
    ("2026-06-23", "Algeria", "Jordan"),
    ("2026-06-23", "Portugal", "Uzbekistan"),
    ("2026-06-24", "Colombia", "Winner Play-off 1"),
    ("2026-06-24", "Canada", "Switzerland"),
    ("2026-06-24", "Winner Play-off A", "Qatar"),
    ("2026-06-26", "United States", "Winner Play-off C"),
    ("2026-06-26", "Paraguay", "Australia"),
    ("2026-06-26", "France", "Norway"),
    ("2026-06-26", "Senegal", "Winner Play-off 2"),
    ("2026-06-27", "Cape Verde", "Saudi Arabia"),
    ("2026-06-27", "Uruguay", "Spain"),
    ("2026-06-27", "Panama", "England"),
    ("2026-06-27", "Croatia", "Ghana"),
}

# Live lookup links (UK + US TV guides)
UK_TV_GUIDE = "https://www.fanzo.com/en/tvguide/football/fifa-world-cup/10105"
US_TV_GUIDE = "https://www.fanzo.com/en-us/tvguide/soccer/fifa-world-cup/10199"
BBC_MATCH_CENTRE = "https://www.bbc.co.uk/sport/football/world-cup/scores-fixtures"
FIFA_SCORES = "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/scores-fixtures"

# --------------------------- Helpers ---------------------------


def is_placeholder(team: str) -> bool:
    """True for knockout placeholders like 'Winner Group L' or 'Runner-up Group D'."""
    return any(
        kw in team
        for kw in ("Winner ", "Runner-up ", "3rd ", "Loser ", "Play-off")
    )


def normalise_team(t: str) -> str:
    """Schedule YAML uses 'play-off X'; ITV release uses 'Play-off X'. Match flexibly."""
    return t.strip()


def uk_broadcaster(match: dict) -> str:
    """Return the UK broadcaster line for a match."""
    stage = match["stage"]
    key = (date_str(match["date"]), normalise_team(match["home_team"]), normalise_team(match["away_team"]))
    if stage == "group":
        if key in ITV_GROUP_MATCHES:
            return "ITV1 / ITVX"
        return "BBC One / iPlayer"
    if stage == "r32":
        return "BBC or ITV (top picks: BBC has 3 of top 5, ITV has 5 of top 9). See UK TV guide."
    if stage == "r16":
        return "BBC or ITV (BBC has 3 of top 4 picks). See UK TV guide."
    if stage == "qf":
        return "BBC or ITV (ITV has top 2 picks + 3 of top picks). See UK TV guide."
    if stage == "sf":
        teams = (match["home_team"], match["away_team"])
        if ENGLAND_TEAM in teams or any("Winner R16" in t for t in teams):
            return "BBC One / iPlayer (England's SF if they qualify) — otherwise check UK TV guide."
        return "BBC or ITV — see UK TV guide."
    if stage == "third_place":
        return "BBC or ITV — see UK TV guide."
    if stage == "final":
        return "BBC One AND ITV1 (shared coverage)"
    return "BBC or ITV"


def us_broadcaster(match: dict) -> str:
    """Return the US broadcaster line for a match."""
    home = match["home_team"]
    away = match["away_team"]
    stage = match["stage"]
    # USA group matches are confirmed on FOX
    if "United States" in (home, away) and stage == "group":
        return "FOX (English) / Telemundo (Spanish)"
    # Hosts opening matches confirmed on FOX
    if stage == "group":
        if (date_str(match["date"]) == "2026-06-11" and "Mexico" in (home, away)):
            return "FOX (English) / Telemundo (Spanish)"
        if (date_str(match["date"]) == "2026-06-12" and "Canada" in (home, away)):
            return "FOX (English) / Telemundo (Spanish)"
    # Final confirmed FOX
    if stage == "final":
        return "FOX (English) / Telemundo (Spanish)"
    # Knockouts: high-profile go on FOX; otherwise check listings
    if stage in ("r16", "qf", "sf", "third_place"):
        return "FOX (English) / Telemundo or Universo (Spanish) — confirm on US TV guide"
    if stage == "r32":
        return "FOX or FS1 (English) / Telemundo or Universo (Spanish) — see US TV guide"
    # Default group stage
    return "FOX or FS1 (English) / Telemundo or Universo (Spanish) — see US TV guide"


def team_tier(match: dict) -> int:
    """Return 1 (England), 2 (marquee), or 3 (other)."""
    teams = (match["home_team"], match["away_team"])
    if ENGLAND_TEAM in teams:
        return 1
    if any(t in MARQUEE_TEAMS for t in teams):
        return 2
    # Knockout matches without England that involve unknown teams are at least tier 2
    if match["stage"] in ("r16", "qf", "sf", "third_place", "final"):
        return 2
    return 3


def stage_label(stage: str) -> str:
    return {
        "group": "Group stage",
        "r32": "Round of 32",
        "r16": "Round of 16",
        "qf": "Quarter-final",
        "sf": "Semi-final",
        "third_place": "Third-place play-off",
        "final": "FINAL",
    }.get(stage, stage)


def event_summary(match: dict, tier: int) -> str:
    home, away = match["home_team"], match["away_team"]
    stage = match["stage"]
    if stage == "final":
        prefix = "FINAL: "
    elif stage == "sf":
        prefix = "SF: "
    elif stage == "qf":
        prefix = "QF: "
    elif stage == "r16":
        prefix = "R16: "
    elif stage == "r32":
        prefix = "R32: "
    elif stage == "third_place":
        prefix = "3rd-place: "
    else:
        prefix = ""
    suffix = ""
    if tier == 1:
        suffix = "  [ENGLAND]"
    return f"WC: {prefix}{home} v {away}{suffix}"


def event_description(match: dict, tier: int) -> str:
    """Build a rich text description per tier."""
    home = match["home_team"]
    away = match["away_team"]
    stage_lbl = stage_label(match["stage"])
    group_lbl = f"Group {match['group']}" if match.get("group") else ""
    venue = f"{match['venue_stadium']}, {match['venue_city']}"
    kick = match["kickoff_uk"]
    uk = uk_broadcaster(match)
    us = us_broadcaster(match)

    if tier == 1:
        # Full briefing for England matches
        return "\n".join([
            f"{stage_lbl}{(' — ' + group_lbl) if group_lbl else ''}",
            f"Kick-off: {kick} UK time",
            f"Venue: {venue}",
            "",
            "── BROADCAST ──",
            f"UK:  {uk}",
            f"US:  {us}",
            "",
            "── LINKS ──",
            f"BBC match centre / lineups: {BBC_MATCH_CENTRE}",
            f"FIFA fixtures hub:          {FIFA_SCORES}",
            f"UK TV guide:                {UK_TV_GUIDE}",
            f"US TV guide:                {US_TV_GUIDE}",
            "",
            "── PREP ──",
            "Lineups confirmed ~60 minutes before kick-off — tap the BBC link above.",
            "Reminders set for 60 min and 15 min before kick-off.",
            "",
            f"(Subscribed feed — regenerated periodically. Generated {GENERATED_AT.strftime('%Y-%m-%d %H:%M UTC')}.)",
        ])

    if tier == 2:
        return "\n".join([
            f"{stage_lbl}{(' — ' + group_lbl) if group_lbl else ''}",
            f"Kick-off: {kick} UK time",
            f"Venue: {venue}",
            "",
            f"UK:  {uk}",
            f"US:  {us}",
            "",
            f"BBC match centre: {BBC_MATCH_CENTRE}",
            f"UK TV guide:      {UK_TV_GUIDE}",
            f"US TV guide:      {US_TV_GUIDE}",
        ])

    # Tier 3 — minimal
    return "\n".join([
        f"{stage_lbl}{(' — ' + group_lbl) if group_lbl else ''}  |  KO {kick} UK",
        f"Venue: {venue}",
        f"UK: {uk}  |  US: {us}",
        f"Live: {BBC_MATCH_CENTRE}",
    ])


def event_categories(match: dict, tier: int) -> str:
    cats = ["World Cup 2026"]
    if match["stage"] == "group":
        cats.append(f"Group {match['group']}")
    else:
        cats.append("Knockout")
    if tier == 1:
        cats.append("England")
    elif tier == 2:
        cats.append("Marquee")
    return ",".join(cats)


def uid_for(match: dict) -> str:
    base = f"wc2026-{match['match_id']}-{date_str(match['date'])}-{match['home_team']}-{match['away_team']}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{match['match_id']}-{h}@wc2026.oliverwakelin"


def ical_escape(s: str) -> str:
    """RFC 5545 text escaping for SUMMARY/DESCRIPTION/LOCATION."""
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace(";", "\\;")
         .replace(",", "\\,")
         .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 line folding: 75 octets per physical line, continuation with single space."""
    out = []
    # Use 73 to keep under the 75 octet limit safely
    while len(line.encode("utf-8")) > 75:
        # find a cut at ~73 chars
        cut = 73
        while cut > 0 and len(line[:cut].encode("utf-8")) > 73:
            cut -= 1
        out.append(line[:cut])
        line = " " + line[cut:]
    out.append(line)
    return "\r\n".join(out)


def _coerce_date(d) -> date:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return date(*map(int, str(d).split("-")))


def fmt_local_dt(d, hhmm: str, add_minutes: int = 0) -> str:
    """Format as TZID local datetime YYYYMMDDTHHMMSS (no Z)."""
    h, m = map(int, hhmm.split(":"))
    base = _coerce_date(d)
    dt = datetime(base.year, base.month, base.day, h, m) + timedelta(minutes=add_minutes)
    return dt.strftime("%Y%m%dT%H%M%S")


def date_str(d) -> str:
    return _coerce_date(d).isoformat()


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------- VTIMEZONE ---------------------------

VTIMEZONE_EUROPE_LONDON = """\
BEGIN:VTIMEZONE
TZID:Europe/London
X-LIC-LOCATION:Europe/London
BEGIN:DAYLIGHT
TZOFFSETFROM:+0000
TZOFFSETTO:+0100
TZNAME:BST
DTSTART:19700329T010000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0100
TZOFFSETTO:+0000
TZNAME:GMT
DTSTART:19701025T020000
RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10
END:STANDARD
END:VTIMEZONE"""


# --------------------------- Event rendering ---------------------------


def render_event(match: dict) -> str:
    tier = team_tier(match)
    uid = uid_for(match)
    dtstart_local = fmt_local_dt(match["date"], match["kickoff_uk"])
    duration = KNOCKOUT_DURATION_MINUTES if match["stage"] != "group" else DEFAULT_DURATION_MINUTES
    dtend_local = fmt_local_dt(match["date"], match["kickoff_uk"], add_minutes=duration)
    dtstamp = fmt_utc(GENERATED_AT)
    summary = ical_escape(event_summary(match, tier))
    description = ical_escape(event_description(match, tier))
    location = ical_escape(f"{match['venue_stadium']}, {match['venue_city']}")
    categories = ical_escape(event_categories(match, tier))

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=Europe/London:{dtstart_local}",
        f"DTEND;TZID=Europe/London:{dtend_local}",
        f"SUMMARY:{summary}",
        f"LOCATION:{location}",
        f"DESCRIPTION:{description}",
        f"CATEGORIES:{categories}",
        f"URL:{FIFA_SCORES}",
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",  # don't block your time — informational
    ]

    if tier == 1:
        # Two alarms: 60 minutes and 15 minutes before kick-off
        for minutes, label in [(60, "60-min reminder"), (15, "15-min reminder")]:
            lines.extend([
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{ical_escape(f'England kick off in {minutes} minutes — {summary}')}",
                f"TRIGGER:-PT{minutes}M",
                "END:VALARM",
            ])

    lines.append("END:VEVENT")
    return "\r\n".join(fold(line) for line in lines)


def render_calendar(schedule: dict) -> str:
    header = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ical_escape(CAL_NAME)}",
        f"X-WR-CALDESC:{ical_escape(CAL_DESC)}",
        f"X-WR-TIMEZONE:{CAL_TIMEZONE}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    parts = ["\r\n".join(fold(line) for line in header), VTIMEZONE_EUROPE_LONDON]

    for match in schedule["matches"]:
        parts.append(render_event(match))

    parts.append("END:VCALENDAR")
    return "\r\n".join(parts) + "\r\n"


# --------------------------- Main ---------------------------


def main() -> int:
    with SCHEDULE_PATH.open() as f:
        schedule = yaml.safe_load(f)

    ics = render_calendar(schedule)
    OUTPUT_PATH.write_text(ics, encoding="utf-8")

    # Quick summary
    total = len(schedule["matches"])
    by_stage: dict[str, int] = {}
    by_tier: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for m in schedule["matches"]:
        by_stage[m["stage"]] = by_stage.get(m["stage"], 0) + 1
        by_tier[team_tier(m)] += 1

    print(f"Wrote {OUTPUT_PATH}")
    print(f"Total events: {total}")
    print(f"By stage: {by_stage}")
    print(f"By tier: Tier 1 (England) = {by_tier[1]}, Tier 2 (marquee/KO) = {by_tier[2]}, Tier 3 = {by_tier[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
