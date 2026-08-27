"""Validation and payload construction for Speedrun.com submissions."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Mapping
from urllib.parse import urlparse


class ValidationError(ValueError):
    """Raised when run details are not valid enough to submit."""


_SECONDS_RE = re.compile(r"^(?P<seconds>\d+)(?:[.,](?P<fraction>\d{1,3}))?$")


def parse_duration_seconds(value: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS (optionally with milliseconds)."""
    parts = (value or "").strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(parts):
        raise ValidationError(
            f'Invalid time "{value}". Use SS, MM:SS, or HH:MM:SS.mmm.'
        )
    match = _SECONDS_RE.fullmatch(parts[-1])
    if not match or any(not part.isdigit() for part in parts[:-1]):
        raise ValidationError(
            f'Invalid time "{value}". Use SS, MM:SS, or HH:MM:SS.mmm.'
        )
    seconds = int(match.group("seconds"))
    if len(parts) > 1 and seconds >= 60:
        raise ValidationError("Seconds must be below 60 when a colon is used.")

    if len(parts) == 3:
        hours, minutes = map(int, parts[:2])
        if minutes >= 60:
            raise ValidationError("Minutes must be below 60 in HH:MM:SS.")
        total = hours * 3600 + minutes * 60 + seconds
    elif len(parts) == 2:
        total = int(parts[0]) * 60 + seconds
    else:
        total = seconds

    fraction = match.group("fraction") or ""
    if fraction:
        total += int(fraction.ljust(3, "0")) / 1000
    if total <= 0:
        raise ValidationError("Run time must be greater than zero.")
    return float(total)


def validate_iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValidationError("Run date must use YYYY-MM-DD.") from exc
    if parsed > date.today():
        raise ValidationError("Run date cannot be in the future.")
    return parsed.isoformat()


def validate_http_url(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"{field_name} must be a complete http:// or https:// URL.")
    return value


def variable_applies(
    variable: Mapping[str, Any],
    category_id: str,
    category_type: str,
    level_id: str | None,
) -> bool:
    """Return whether a game variable applies to the chosen leaderboard."""
    restricted_category = variable.get("category")
    if restricted_category and restricted_category != category_id:
        return False

    scope = variable.get("scope") or {}
    scope_type = scope.get("type", "global")
    if scope_type == "global":
        return True
    if scope_type == "full-game":
        return category_type == "per-game"
    if scope_type == "all-levels":
        return category_type == "per-level"
    if scope_type == "single-level":
        return category_type == "per-level" and scope.get("level") == level_id
    return False


def build_run_payload(
    *,
    category_id: str,
    category_type: str,
    level_id: str | None,
    run_date: str,
    times: Mapping[str, str],
    allowed_times: list[str],
    platform_id: str | None = None,
    region_id: str | None = None,
    emulated: bool = False,
    emulators_allowed: bool = True,
    video: str = "",
    video_required: bool = False,
    comment: str = "",
    splitsio: str = "",
    variables: Mapping[str, tuple[str, str]] | None = None,
    players: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate run data and build the Speedrun.com POST /runs body."""
    if not category_id:
        raise ValidationError("Choose a category.")
    if category_type == "per-level" and not level_id:
        raise ValidationError("Choose a level for this individual-level category.")

    parsed_times: dict[str, float] = {}
    for timing_method, raw_value in times.items():
        raw_value = (raw_value or "").strip()
        if not raw_value:
            continue
        if timing_method not in allowed_times:
            raise ValidationError(f"{timing_method} is not allowed for this game.")
        parsed_times[timing_method] = parse_duration_seconds(raw_value)
    if not parsed_times:
        raise ValidationError("Enter at least one run time.")

    if emulated and not emulators_allowed:
        raise ValidationError("This game does not allow emulator runs.")

    run: dict[str, Any] = {
        "category": category_id,
        "date": validate_iso_date(run_date),
        "times": parsed_times,
        "emulated": bool(emulated),
    }
    if category_type == "per-level":
        run["level"] = level_id
    if platform_id:
        run["platform"] = platform_id
    if region_id:
        run["region"] = region_id
    if players:
        normalized_players = []
        for player in players:
            player_id = str(player.get("id") or "").strip()
            if player.get("rel") != "user" or not player_id:
                raise ValidationError("Run players must be registered Speedrun.com users.")
            normalized_players.append({"rel": "user", "id": player_id})
        run["players"] = normalized_players

    video_url = validate_http_url(video, "Video URL")
    if video_required and not video_url:
        raise ValidationError("This game requires a video URL.")
    if video_url:
        run["video"] = video_url
    splits_url_or_id = (splitsio or "").strip()
    if splits_url_or_id:
        if "://" in splits_url_or_id:
            validate_http_url(splits_url_or_id, "Splits.io")
        run["splitsio"] = splits_url_or_id
    if (comment or "").strip():
        run["comment"] = comment.strip()

    variable_payload: dict[str, dict[str, str]] = {}
    for variable_id, selection in (variables or {}).items():
        variable_type, value = selection
        value = (value or "").strip()
        if value:
            variable_payload[variable_id] = {"type": variable_type, "value": value}
    if variable_payload:
        run["variables"] = variable_payload

    return {"run": run}
