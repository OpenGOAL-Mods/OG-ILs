"""Small, explicit client for the speedrun.com API v1."""

from __future__ import annotations

from typing import Any

import requests

from . import __version__


class SpeedrunAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SpeedrunAPI:
    BASE_URL = "https://www.speedrun.com/api/v1"

    def __init__(self, api_key: str = "", *, timeout: float = 20.0) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"opengoal-replay-server/{__version__}",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            if not self.api_key:
                raise SpeedrunAPIError("Enter your speedrun.com API key first.")
            headers["X-API-Key"] = self.api_key

        try:
            response = self.session.request(
                method,
                f"{self.BASE_URL}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise SpeedrunAPIError("speedrun.com took too long to respond. Try again.") from exc
        except requests.RequestException as exc:
            raise SpeedrunAPIError(f"Could not reach speedrun.com: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = None

        if not response.ok:
            raise SpeedrunAPIError(
                self._error_message(response.status_code, body),
                status_code=response.status_code,
            )
        if not isinstance(body, dict) or "data" not in body:
            raise SpeedrunAPIError("speedrun.com returned an unexpected response.")
        return body["data"]

    @staticmethod
    def _error_message(status_code: int, body: Any) -> str:
        if status_code in {401, 403}:
            return "The API key was rejected or does not have permission for this action."
        if status_code == 404:
            return "The requested speedrun.com item could not be found."
        if status_code == 420:
            return "speedrun.com's rate limit was reached. Wait a minute and try again."
        if isinstance(body, dict):
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                return "Submission was rejected:\n" + "\n".join(f"• {item}" for item in errors)
            message = body.get("message")
            if message:
                return str(message)
        return f"speedrun.com returned HTTP {status_code}."

    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile", authenticated=True)

    def game_details(self, game_id: str) -> dict[str, Any]:
        return self._request("GET", f"/games/{game_id}")

    def game_categories(self, game_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/games/{game_id}/categories")

    def game_levels(self, game_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/games/{game_id}/levels")

    def game_variables(self, game_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/games/{game_id}/variables")

    def game_runners(self, game_id: str) -> list[dict[str, Any]]:
        """Return registered users with verified runs for a game."""
        runners: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            runs = self._request(
                "GET",
                "/runs",
                params={
                    "game": game_id,
                    "status": "verified",
                    "embed": "players",
                    "max": 200,
                    "offset": offset,
                },
            )
            for run in runs:
                players = run.get("players") or []
                if isinstance(players, dict):
                    players = players.get("data") or []
                for player in players:
                    if player.get("rel") != "user" or not player.get("id"):
                        continue
                    runners[str(player["id"])] = player
            if len(runs) < 200:
                break
            offset += len(runs)
        return sorted(
            runners.values(),
            key=lambda player: str((player.get("names") or {}).get("international") or "").casefold(),
        )

    def platforms(self) -> list[dict[str, Any]]:
        return self._request("GET", "/platforms", params={"max": 200})

    def regions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/regions", params={"max": 100})

    def load_game_form(self, game_id: str) -> dict[str, Any]:
        """Fetch everything needed to populate a submission."""
        game = self.game_details(game_id)
        platform_ids = set(game.get("platforms") or [])
        region_ids = set(game.get("regions") or [])
        return {
            "game": game,
            "categories": self.game_categories(game_id),
            "levels": self.game_levels(game_id),
            "variables": self.game_variables(game_id),
            "platforms": [p for p in self.platforms() if p.get("id") in platform_ids],
            "regions": [r for r in self.regions() if r.get("id") in region_ids],
        }

    def submit_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/runs", authenticated=True, json=payload)
