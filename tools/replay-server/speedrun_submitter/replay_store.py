"""Persistent storage and speedrun.com submission for the local replay server."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import secrets
import threading
from typing import Any

from .api import SpeedrunAPI, SpeedrunAPIError
from .logic import build_run_payload, variable_applies


GAME_ID = "j1l7q0zd"
PC_PLATFORM_ID = "8gej2n93"
PROOF_VIDEO_URL = (
    "https://youtube.com/watch?v=hJZF4iOhbgY&time_continue=1&source_ve_path=MjM4NTE"
    "&embeds_referring_euri=https%3A%2F%2Fwww.speedrun.com%2F"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7878

REPLAY_MODES = [
    {
        "id": "default",
        "label": "Default - Next Place",
        "description": (
            "Start with the slowest completed ghost, then race the next faster time "
            "as your personal best improves."
        ),
    },
    {
        "id": "next_three",
        "label": "Default - Next 3 Places",
        "description": (
            "Start with the three slowest completed ghosts, then race the three "
            "closest faster times as your personal best improves."
        ),
    },
    {
        "id": "personal_best",
        "label": "Race vs Your Best",
        "description": "Race your fastest completed replay for this mission.",
    },
    {
        "id": "world_record",
        "label": "Race vs WR",
        "description": "Race the fastest completed replay available on this server.",
    },
    {
        "id": "last_attempt",
        "label": "Race vs Last Attempt",
        "description": "Race your newest attempt, including an unfinished retry.",
    },
    {
        "id": "custom",
        "label": "Custom",
        "description": "Race any combination of manually selected mission replays.",
    },
]
REPLAY_MODE_IDS = {mode["id"] for mode in REPLAY_MODES}


def default_data_dir() -> Path:
    override = os.environ.get("OPENGOAL_REPLAY_DATA", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "OpenGOAL"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "OpenGOAL"
    return base / "jak3" / "replay-server"


def _display_name(item: dict[str, Any]) -> str:
    names = item.get("names") or {}
    return str(names.get("international") or item.get("name") or item.get("id", "Unknown"))


class ReplayStore:
    """Thread-safe JSON index plus immutable replay payload files."""

    def __init__(self, root: Path | None = None, *, api_factory=SpeedrunAPI) -> None:
        self.root = root or default_data_dir()
        self.replay_dir = self.root / "replays"
        self.index_path = self.root / "index.json"
        self._api_factory = api_factory
        self._lock = threading.RLock()
        self._src_form: dict[str, Any] | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 6,
            "replays": [],
            "players": [],
            "player_settings": {},
            "last_unknown_player_ping": {},
            "moderator": {},
            "runners": [],
            "settings": {
                "selected_replay_ids": [],
                "selected_runner_id": "",
                "auto_submit": True,
                "replay_mode": "default",
            },
        }

    def _load(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_state()
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("index is not an object")
            base = self._empty_state()
            incoming_settings = data.get("settings") or {}
            for key in (
                "replays",
                "players",
                "last_unknown_player_ping",
                "moderator",
                "runners",
            ):
                if key in data:
                    base[key] = data[key]
            if isinstance(data.get("player_settings"), dict):
                base["player_settings"] = data["player_settings"]
            for replay in base["replays"]:
                if isinstance(replay, dict):
                    replay.setdefault("game", "jak3")
                    replay.setdefault("player_id", "")
                    replay.setdefault("src_runner_id", "")
                    replay.setdefault("is_personal_best", False)
                    replay.setdefault("completed", True)
            for player in base["players"]:
                if isinstance(player, dict):
                    player.setdefault("src_runner_id", "")
                    player.setdefault("first_seen", "")
                    player.setdefault("last_seen", player["first_seen"])
            known_players = {
                str(player.get("id") or "")
                for player in base["players"]
                if isinstance(player, dict)
            }
            for replay in base["replays"]:
                player_id = str(replay.get("player_id") or "")
                if player_id and player_id not in known_players:
                    base["players"].append({
                        "id": player_id,
                        "src_runner_id": str(replay.get("src_runner_id") or ""),
                        "first_seen": str(replay.get("created_at") or ""),
                        "last_seen": str(replay.get("created_at") or ""),
                    })
                    known_players.add(player_id)
            for key in (
                "selected_replay_ids",
                "selected_runner_id",
                "auto_submit",
                "replay_mode",
            ):
                if key in incoming_settings:
                    base["settings"][key] = incoming_settings[key]
            if base["settings"].get("replay_mode") not in REPLAY_MODE_IDS:
                base["settings"]["replay_mode"] = "default"
            # Migrate the original single-opponent setting without losing the choice.
            if "selected_replay_ids" not in incoming_settings:
                selected = str(incoming_settings.get("selected_replay_id") or "")
                base["settings"]["selected_replay_ids"] = [selected] if selected else []
            replay_ids = {
                str(replay.get("id") or "")
                for replay in base["replays"]
                if isinstance(replay, dict)
            }
            normalized_player_settings: dict[str, dict[str, Any]] = {}
            for player_id, settings in base["player_settings"].items():
                if not isinstance(player_id, str) or not isinstance(settings, dict):
                    continue
                replay_mode = str(settings.get("replay_mode") or "default")
                if replay_mode not in REPLAY_MODE_IDS:
                    replay_mode = "default"
                selected_replays: list[str] = []
                if isinstance(settings.get("selected_replay_ids"), list):
                    for value in settings["selected_replay_ids"]:
                        replay_id = str(value or "")
                        if replay_id in replay_ids and replay_id not in selected_replays:
                            selected_replays.append(replay_id)
                normalized_player_settings[player_id] = {
                    "replay_mode": replay_mode,
                    "selected_replay_ids": selected_replays,
                }
            base["player_settings"] = normalized_player_settings
            return base
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            backup = self.index_path.with_suffix(".corrupt.json")
            try:
                self.index_path.replace(backup)
            except OSError:
                pass
            return self._empty_state()

    def _save(self) -> None:
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.index_path)

    def public_state(self, player_id: str = "") -> dict[str, Any]:
        with self._lock:
            state = deepcopy(self._state)
            if player_id:
                player_settings = self._state["player_settings"].get(player_id, {})
                state["settings"]["replay_mode"] = player_settings.get(
                    "replay_mode", state["settings"]["replay_mode"]
                )
                state["settings"]["selected_replay_ids"] = deepcopy(
                    player_settings.get(
                        "selected_replay_ids",
                        state["settings"]["selected_replay_ids"],
                    )
                )
        state.pop("player_settings", None)
        if not isinstance(state.get("moderator"), dict):
            state["moderator"] = {}
        state["moderator"].pop("api_key", None)
        state["replay_modes"] = deepcopy(REPLAY_MODES)
        state["replays"].sort(key=lambda replay: replay.get("created_at", ""), reverse=True)
        return state

    def _find(self, collection: str, item_id: str) -> dict[str, Any]:
        for item in self._state[collection]:
            if item.get("id") == item_id:
                return item
        raise KeyError(item_id)

    def _clear_removed_runner_mappings(self, valid_runner_ids: set[str]) -> None:
        removed_player_ids: set[str] = set()
        for player in self._state["players"]:
            runner_id = str(player.get("src_runner_id") or "")
            if runner_id and runner_id not in valid_runner_ids:
                player["src_runner_id"] = ""
                removed_player_ids.add(str(player.get("id") or ""))
        for replay in self._state["replays"]:
            if (
                replay.get("player_id") in removed_player_ids
                and replay.get("src_status") not in {"queued", "submitting", "submitted"}
            ):
                replay["src_runner_id"] = ""

    @staticmethod
    def _validated_player_id(value: Any) -> str:
        player_id = str(value or "").strip()
        if not (16 <= len(player_id) <= 128) or not all(
            character.isalnum() or character in "-_" for character in player_id
        ):
            raise ValueError("player_id is invalid")
        return player_id

    def record_unknown_player_ping(self, player_id: str) -> dict[str, str]:
        """Record an out-of-band identity check from an unmapped game install."""
        player_id = self._validated_player_id(player_id)
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            try:
                player = self._find("players", player_id)
                if str(player.get("src_runner_id") or ""):
                    raise ValueError("player_id is already mapped to an SRC runner")
                player["last_seen"] = now
            except KeyError:
                player = {
                    "id": player_id,
                    "src_runner_id": "",
                    "first_seen": now,
                    "last_seen": now,
                }
                self._state["players"].append(player)
            ping = {"player_id": player_id, "pinged_at": now}
            self._state["last_unknown_player_ping"] = ping
            self._save()
            return deepcopy(ping)

    def add_replay(self, envelope: dict[str, Any]) -> dict[str, Any]:
        replay = envelope.get("replay")
        game = str(envelope.get("game") or "jak3").strip().lower()
        category = str(envelope.get("category") or "").strip()
        player_id = str(envelope.get("player_id") or "").strip()
        seconds = envelope.get("time_seconds")
        if not category or not player_id or not isinstance(replay, dict):
            raise ValueError("category, player_id, and replay are required")
        if not game or len(game) > 32 or not all(
            character.isalnum() or character in "-_" for character in game
        ):
            raise ValueError("game is invalid")
        player_id = self._validated_player_id(player_id)
        if replay.get("category") != category:
            raise ValueError("replay category does not match the upload category")
        frames = replay.get("frames")
        count = replay.get("count")
        if not isinstance(frames, list) or not isinstance(count, int) or count != len(frames):
            raise ValueError("replay frame count is invalid")
        try:
            seconds = float(seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("time_seconds must be a number") from exc
        if seconds <= 0:
            raise ValueError("time_seconds must be greater than zero")

        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            try:
                player = self._find("players", player_id)
                player["last_seen"] = now
            except KeyError:
                player = {
                    "id": player_id,
                    "src_runner_id": "",
                    "first_seen": now,
                    "last_seen": now,
                }
                self._state["players"].append(player)
            known_ids = {item["id"] for item in self._state["replays"]}
            replay_id = secrets.token_hex(16)
            while replay_id in known_ids:
                replay_id = secrets.token_hex(16)
            replay_path = self.replay_dir / f"{replay_id}.json"
            replay_path.write_text(json.dumps(replay, separators=(",", ":")), encoding="utf-8")
            metadata = {
                "id": replay_id,
                "game": game,
                "player_id": player_id,
                "display_name": str(envelope.get("display_name") or "").strip()
                or f"{category} — {seconds:.3f}s"
                + (" PB" if envelope.get("is_personal_best") is True else ""),
                "category": category,
                "time_seconds": seconds,
                "is_personal_best": (
                    envelope.get("is_personal_best") is True
                    and envelope.get("completed") is not False
                ),
                "completed": envelope.get("completed") is not False,
                "frame_count": count,
                "mod_version": str(replay.get("mod_version") or ""),
                "created_at": now,
                "src_level_id": str(envelope.get("src_level_id") or ""),
                "src_category_id": str(envelope.get("src_category_id") or ""),
                "src_variable_labels": dict(envelope.get("src_variable_labels") or {}),
                "src_runner_id": str(player.get("src_runner_id") or ""),
                "src_status": "not_requested",
                "src_run_id": "",
                "src_run_url": "",
                "src_error": "",
            }
            self._state["replays"].append(metadata)
            self._save()

        if self.should_auto_submit(replay_id):
            self.submit_async(replay_id)
        return deepcopy(metadata)

    def replay_bytes(self, replay_id: str) -> bytes:
        with self._lock:
            self._find("replays", replay_id)
        return (self.replay_dir / f"{replay_id}.json").read_bytes()

    def rename_replay(self, replay_id: str, display_name: str) -> dict[str, Any]:
        display_name = display_name.strip()
        if not display_name or len(display_name) > 120:
            raise ValueError("display_name must be between 1 and 120 characters")
        with self._lock:
            replay = self._find("replays", replay_id)
            replay["display_name"] = display_name
            self._save()
            return deepcopy(replay)

    def resolve_replay_selection(
        self, category: str, player_id: str
    ) -> dict[str, Any]:
        """Resolve the active strategy to replay metadata for one mission/player."""
        category = category.strip()
        player_id = player_id.strip()
        if not category or not player_id:
            raise ValueError("category and player_id are required")
        with self._lock:
            player_settings = self._state["player_settings"].get(player_id, {})
            mode = str(
                player_settings.get(
                    "replay_mode", self._state["settings"].get("replay_mode")
                )
                or "default"
            )
            mission = [
                replay
                for replay in self._state["replays"]
                if replay.get("category") == category
            ]
            completed = [replay for replay in mission if replay.get("completed", True)]
            own = [replay for replay in mission if replay.get("player_id") == player_id]
            own_completed = [replay for replay in own if replay.get("completed", True)]
            selected: list[dict[str, Any]] = []

            if mode == "custom":
                mission_by_id = {replay.get("id"): replay for replay in mission}
                selected = [
                    mission_by_id[replay_id]
                    for replay_id in player_settings.get(
                        "selected_replay_ids",
                        self._state["settings"].get("selected_replay_ids") or [],
                    )
                    if replay_id in mission_by_id
                ]
            elif mode == "personal_best" and own_completed:
                selected = [min(own_completed, key=lambda replay: replay["time_seconds"])]
            elif mode == "world_record" and completed:
                selected = [min(completed, key=lambda replay: replay["time_seconds"])]
            elif mode == "last_attempt" and own:
                selected = [max(own, key=lambda replay: replay.get("created_at", ""))]
            elif mode == "default" and completed:
                if not own_completed:
                    selected = [max(completed, key=lambda replay: replay["time_seconds"])]
                else:
                    personal_best = min(
                        replay["time_seconds"] for replay in own_completed
                    )
                    faster = [
                        replay
                        for replay in completed
                        if replay["time_seconds"] < personal_best
                    ]
                    selected = [
                        max(faster, key=lambda replay: replay["time_seconds"])
                        if faster
                        else min(completed, key=lambda replay: replay["time_seconds"])
                    ]
            elif mode == "next_three" and completed:
                if not own_completed:
                    selected = sorted(
                        completed,
                        key=lambda replay: replay["time_seconds"],
                        reverse=True,
                    )[:3]
                else:
                    personal_best = min(
                        replay["time_seconds"] for replay in own_completed
                    )
                    faster = sorted(
                        (
                            replay
                            for replay in completed
                            if replay["time_seconds"] < personal_best
                        ),
                        key=lambda replay: replay["time_seconds"],
                        reverse=True,
                    )
                    selected = (
                        faster[:3]
                        if faster
                        else [min(completed, key=lambda replay: replay["time_seconds"])]
                    )

            return {
                "mode": mode,
                "replays": deepcopy(selected),
            }

    def assign_replay_runner(self, replay_id: str, runner_id: str) -> dict[str, Any]:
        runner_id = runner_id.strip()
        with self._lock:
            replay = self._find("replays", replay_id)
            if runner_id:
                self._find("runners", runner_id)
            current = str(replay.get("src_runner_id") or "")
            if current != runner_id and replay.get("src_status") in {
                "queued",
                "submitting",
                "submitted",
            }:
                raise ValueError("The runner cannot be changed after submission has started")
            replay["src_runner_id"] = runner_id
            if replay.get("src_status") == "failed":
                replay.update(src_status="not_requested", src_error="")
            self._save()
            return deepcopy(replay)

    def assign_player_runner(self, player_id: str, runner_id: str) -> dict[str, Any]:
        """Map one stable game installation ID to an SRC runner.

        The mapping is copied onto every replay that has not started submission, and
        future uploads inherit it automatically.
        """
        player_id = player_id.strip()
        runner_id = runner_id.strip()
        auto_submit_ids: list[str] = []
        with self._lock:
            player = self._find("players", player_id)
            if runner_id:
                self._find("runners", runner_id)
            player["src_runner_id"] = runner_id
            newest_by_category: dict[str, dict[str, Any]] = {}
            for replay in self._state["replays"]:
                if replay.get("player_id") != player_id:
                    continue
                if replay.get("src_status") in {"queued", "submitting", "submitted"}:
                    continue
                previous_runner = str(replay.get("src_runner_id") or "")
                replay["src_runner_id"] = runner_id
                if previous_runner != runner_id and replay.get("src_status") == "failed":
                    replay.update(src_status="not_requested", src_error="")
                if replay.get("is_personal_best") and replay.get("src_status") == "not_requested":
                    category = str(replay.get("category") or "")
                    current = newest_by_category.get(category)
                    if current is None or replay.get("created_at", "") > current.get("created_at", ""):
                        newest_by_category[category] = replay
            if (
                runner_id
                and self._state["settings"].get("auto_submit")
                and self._state["moderator"].get("api_key")
            ):
                auto_submit_ids = [replay["id"] for replay in newest_by_category.values()]
            self._save()
            result = deepcopy(player)
        for replay_id in auto_submit_ids:
            self.submit_async(replay_id)
        return result

    def configure_moderator(self, api_key: str) -> dict[str, Any]:
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("api_key is required")
        api = self._api_factory(api_key)
        profile = api.profile()
        user_id = str(profile.get("id") or "")
        if not user_id:
            raise SpeedrunAPIError("speedrun.com returned a profile without a user ID.")
        game = api.game_details(GAME_ID)
        moderators = game.get("moderators") or {}
        global_role = str(profile.get("role") or "")
        if user_id not in moderators and global_role not in {"moderator", "admin", "programmer"}:
            raise SpeedrunAPIError(
                "This account is not a moderator for the Jak 3 OpenGOAL Missions board."
            )
        moderator = {
            "user_id": user_id,
            "display_name": _display_name(profile),
            "api_key": api_key,
        }
        runners = self._load_runners(api)
        with self._lock:
            self._state["moderator"] = moderator
            self._state["runners"] = runners
            selected = self._state["settings"].get("selected_runner_id", "")
            if selected and not any(runner["id"] == selected for runner in runners):
                self._state["settings"]["selected_runner_id"] = ""
            valid_runner_ids = {runner["id"] for runner in runners}
            self._clear_removed_runner_mappings(valid_runner_ids)
            self._save()
        result = deepcopy(moderator)
        result.pop("api_key", None)
        return result

    def _load_runners(self, api: SpeedrunAPI) -> list[dict[str, str]]:
        return [
            {"id": str(player["id"]), "display_name": _display_name(player)}
            for player in api.game_runners(GAME_ID)
            if player.get("id")
        ]

    def refresh_runners(self) -> list[dict[str, str]]:
        with self._lock:
            moderator = deepcopy(self._state["moderator"])
        if not moderator.get("api_key"):
            raise ValueError("Configure the moderator API key first")
        runners = self._load_runners(self._api_factory(moderator["api_key"]))
        with self._lock:
            self._state["runners"] = runners
            selected = self._state["settings"].get("selected_runner_id", "")
            if selected and not any(runner["id"] == selected for runner in runners):
                self._state["settings"]["selected_runner_id"] = ""
            valid_runner_ids = {runner["id"] for runner in runners}
            self._clear_removed_runner_mappings(valid_runner_ids)
            self._save()
        return deepcopy(runners)

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "selected_replay_ids" in values:
                replay_ids = values["selected_replay_ids"]
                if not isinstance(replay_ids, list):
                    raise ValueError("selected_replay_ids must be a list")
                normalized: list[str] = []
                for value in replay_ids:
                    replay_id = str(value or "")
                    if not replay_id or replay_id in normalized:
                        continue
                    self._find("replays", replay_id)
                    normalized.append(replay_id)
                self._state["settings"]["selected_replay_ids"] = normalized
            if "selected_runner_id" in values:
                runner_id = str(values["selected_runner_id"] or "")
                if runner_id:
                    self._find("runners", runner_id)
                self._state["settings"]["selected_runner_id"] = runner_id
            if "auto_submit" in values:
                self._state["settings"]["auto_submit"] = bool(values["auto_submit"])
            if "replay_mode" in values:
                replay_mode = str(values["replay_mode"] or "")
                if replay_mode not in REPLAY_MODE_IDS:
                    raise ValueError("replay_mode is invalid")
                self._state["settings"]["replay_mode"] = replay_mode
            self._save()
            return deepcopy(self._state["settings"])

    def update_player_settings(
        self, player_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        player_id = player_id.strip()
        if not player_id or len(player_id) > 128 or not all(
            character.isalnum() or character in "-_" for character in player_id
        ):
            raise ValueError("player_id is invalid")
        with self._lock:
            settings = self._state["player_settings"].setdefault(
                player_id,
                {
                    "replay_mode": self._state["settings"]["replay_mode"],
                    "selected_replay_ids": deepcopy(
                        self._state["settings"]["selected_replay_ids"]
                    ),
                },
            )
            if "selected_replay_ids" in values:
                replay_ids = values["selected_replay_ids"]
                if not isinstance(replay_ids, list):
                    raise ValueError("selected_replay_ids must be a list")
                normalized: list[str] = []
                for value in replay_ids:
                    replay_id = str(value or "")
                    if not replay_id or replay_id in normalized:
                        continue
                    self._find("replays", replay_id)
                    normalized.append(replay_id)
                settings["selected_replay_ids"] = normalized
            if "replay_mode" in values:
                replay_mode = str(values["replay_mode"] or "")
                if replay_mode not in REPLAY_MODE_IDS:
                    raise ValueError("replay_mode is invalid")
                settings["replay_mode"] = replay_mode
            self._save()
            return deepcopy(settings)

    def should_auto_submit(self, replay_id: str) -> bool:
        with self._lock:
            settings = self._state["settings"]
            replay = self._find("replays", replay_id)
            return bool(
                settings.get("auto_submit")
                and replay.get("is_personal_best")
                and replay.get("src_runner_id")
                and self._state["moderator"].get("api_key")
            )

    def submit_async(self, replay_id: str) -> None:
        with self._lock:
            replay = self._find("replays", replay_id)
            if replay.get("src_status") in {"queued", "submitting", "submitted"}:
                return
            if not replay.get("is_personal_best"):
                raise ValueError("Only personal-best replays can be submitted to Speedrun.com")
            runner_id = str(replay.get("src_runner_id") or "")
            if not self._state["moderator"].get("api_key"):
                raise ValueError("Configure the moderator API key first")
            if not runner_id:
                raise ValueError("Assign a Speedrun.com runner to this replay first")
            self._find("runners", runner_id)
            replay["src_status"] = "queued"
            replay["src_error"] = ""
            self._save()
        threading.Thread(target=self._submit, args=(replay_id,), daemon=True).start()

    def _submission_variables(
        self, variables: list[dict[str, Any]], category_id: str, level_id: str,
        mod_version: str, requested_labels: dict[str, str]
    ) -> dict[str, tuple[str, str]]:
        selections: dict[str, tuple[str, str]] = {}
        normalized_version = mod_version.removeprefix("v").strip()
        for variable in variables:
            if not variable_applies(variable, category_id, "per-level", level_id):
                continue
            variable_id = str(variable.get("id") or "")
            if not variable_id:
                continue
            if variable.get("user-defined"):
                if variable.get("mandatory"):
                    selections[variable_id] = ("user-defined", mod_version or "unknown")
                continue
            values = (variable.get("values") or {}).get("values") or {}
            chosen = ""
            requested = requested_labels.get(str(variable.get("name") or ""), "")
            if requested:
                chosen = next(
                    (value_id for value_id, details in values.items()
                     if str(details.get("label") or "").casefold() == requested.casefold()), ""
                )
                if not chosen:
                    raise ValueError(
                        f"{requested} is not configured for Speedrun.com variable {variable.get('name', variable_id)}"
                    )
            for value_id, details in values.items():
                if chosen:
                    break
                label = str(details.get("label") or "")
                if label.removeprefix("v").strip() == normalized_version and normalized_version:
                    chosen = value_id
                    break
            if not chosen and "version" in str(variable.get("name") or "").casefold():
                raise ValueError(f"Mod version {mod_version or '(unknown)'} is not configured on Speedrun.com")
            if not chosen:
                default = (variable.get("values") or {}).get("default")
                if default in values:
                    chosen = default
            if not chosen and variable.get("mandatory"):
                chosen = next(
                    (value_id for value_id, details in values.items() if str(details.get("label", "")).lower() == "n/a"),
                    "",
                )
            if chosen:
                selections[variable_id] = ("pre-defined", chosen)
            elif variable.get("mandatory"):
                raise ValueError(f"No automatic value is available for required variable {variable.get('name', variable_id)}")
        return selections

    def _submit(self, replay_id: str) -> None:
        try:
            with self._lock:
                replay = deepcopy(self._find("replays", replay_id))
                moderator = deepcopy(self._state["moderator"])
                runner_id = str(replay.get("src_runner_id") or "")
                runner = deepcopy(self._find("runners", runner_id))
                live = self._find("replays", replay_id)
                live["src_status"] = "submitting"
                self._save()
            if not replay.get("src_level_id") or not replay.get("src_category_id"):
                raise ValueError("This replay does not have a Speedrun.com level/category mapping")
            if not moderator.get("api_key"):
                raise ValueError("The replay server does not have a moderator API key")
            api = self._api_factory(moderator["api_key"])
            with self._lock:
                if self._src_form is None:
                    self._src_form = api.load_game_form(GAME_ID)
                form = deepcopy(self._src_form)
            ruleset = form["game"].get("ruleset") or {}
            allowed_times = ruleset.get("run-times") or ["realtime"]
            default_time = str(ruleset.get("default-time") or "")
            timing_method = default_time if default_time in allowed_times else allowed_times[0]
            variables = self._submission_variables(
                form.get("variables", []), replay["src_category_id"], replay["src_level_id"],
                replay.get("mod_version", ""), replay.get("src_variable_labels", {})
            )
            platforms = form.get("platforms") or []
            platform_id = next(
                (platform.get("id") for platform in platforms if platform.get("id") == PC_PLATFORM_ID),
                platforms[0].get("id") if platforms else None,
            )
            payload = build_run_payload(
                category_id=replay["src_category_id"], category_type="per-level",
                level_id=replay["src_level_id"], run_date=date.today().isoformat(),
                times={timing_method: str(replay["time_seconds"])}, allowed_times=allowed_times,
                platform_id=platform_id, emulated=False,
                emulators_allowed=ruleset.get("emulators-allowed", True),
                video=PROOF_VIDEO_URL,
                video_required=ruleset.get("require-video", False),
                comment=(
                    "Automatically submitted by OpenGOAL Replay Server. "
                    f"Player ID: {replay.get('player_id', 'legacy')}; Replay ID: {replay_id}"
                ),
                variables=variables,
                players=[{"rel": "user", "id": runner["id"]}],
            )
            result = api.submit_run(payload)
            with self._lock:
                live = self._find("replays", replay_id)
                live.update(src_status="submitted", src_run_id=str(result.get("id") or ""),
                            src_run_url=str(result.get("weblink") or ""), src_error="")
                self._save()
        except Exception as exc:
            with self._lock:
                live = self._find("replays", replay_id)
                live.update(src_status="failed", src_error=str(exc))
                self._save()
