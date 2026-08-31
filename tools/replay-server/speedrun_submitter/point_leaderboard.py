"""Cached parser for the public JakMods individual-level point standings."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import threading
import time
from typing import Callable, Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LEADERBOARD_ORIGIN = "https://im.jakmods.dev/"
LEADERBOARD_MODES = {
    "jak2": "Jak II",
    "jak3": "Jak 3",
    "combined": "Combined",
}
LEADERBOARD_GROUPS = {
    "all": "Overall",
    "main": "Main Missions",
    "orb": "Orb Searches",
    "side": "Other Side Missions",
}


class PointLeaderboardError(RuntimeError):
    """Raised when the upstream points site cannot provide usable standings."""


class _StandingsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_row = False
        self.cell_parts: list[str] | None = None
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and attributes.get("id") == "leaderboard-table":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag == "td":
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.cell_parts is not None:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_row and tag == "td" and self.cell_parts is not None:
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.cell_parts = None
        elif self.in_table and tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
            self.row = []
            self.cell_parts = None
        elif self.in_table and tag == "table":
            self.in_table = False


def parse_point_standings(page: str) -> list[dict[str, Any]]:
    parser = _StandingsTableParser()
    parser.feed(page)
    standings: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) < 6:
            continue
        rank_match = re.search(r"\d+", cells[0])
        missions_match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", cells[3])
        if not rank_match or not missions_match:
            continue
        try:
            standings.append(
                {
                    "rank": int(rank_match.group()),
                    "rank_label": cells[0],
                    "runner": cells[1],
                    "points": int(cells[2].replace(",", "")),
                    "missions_run": int(missions_match.group(1)),
                    "missions_total": int(missions_match.group(2)),
                    "tied_wrs": int(cells[4].replace(",", "")),
                    "untied_wrs": int(cells[5].replace(",", "")),
                }
            )
        except ValueError:
            continue
    if not standings:
        raise PointLeaderboardError("JakMods returned no point standings")
    return standings


def _download(url: str) -> str:
    request = Request(url, headers={"User-Agent": "OpenGOAL-Replay-Server/1.0"})
    try:
        with urlopen(request, timeout=8) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        raise PointLeaderboardError(f"Could not load JakMods point standings: {exc}") from exc


class PointLeaderboardClient:
    def __init__(
        self,
        fetch: Callable[[str], str] | None = None,
        *,
        cache_seconds: float = 300.0,
    ) -> None:
        self._fetch = fetch or _download
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def standings(self, mode: str, group: str) -> dict[str, Any]:
        if mode not in LEADERBOARD_MODES:
            raise ValueError("Unknown point-leaderboard game")
        if group not in LEADERBOARD_GROUPS:
            raise ValueError("Unknown point-leaderboard category")
        key = (mode, group)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self._cache_seconds:
                return deepcopy(cached[1])

        query: dict[str, str] = {"mode": mode}
        if group != "all":
            query["group"] = group
        source_url = f"{LEADERBOARD_ORIGIN}?{urlencode(query)}"
        entries = parse_point_standings(self._fetch(source_url))
        payload = {
            "version": 1,
            "mode": mode,
            "mode_label": LEADERBOARD_MODES[mode],
            "group": group,
            "group_label": LEADERBOARD_GROUPS[group],
            "source_url": source_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        }
        with self._lock:
            self._cache[key] = (time.monotonic(), deepcopy(payload))
        return payload
