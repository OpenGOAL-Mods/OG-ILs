from unittest.mock import Mock
import unittest

from speedrun_submitter.api import SpeedrunAPI
from speedrun_submitter.logic import ValidationError, build_run_payload


class SubmissionTests(unittest.TestCase):
    def test_payload_submits_as_mapped_registered_runner(self):
        payload = build_run_payload(
            category_id="category",
            category_type="per-level",
            level_id="level",
            run_date="2024-03-01",
            times={"ingame": "8.125"},
            allowed_times=["ingame"],
            video="https://youtube.com/watch?v=hJZF4iOhbgY",
            players=[{"rel": "user", "id": "runner-1"}],
        )
        self.assertEqual(payload["run"]["players"], [{"rel": "user", "id": "runner-1"}])

    def test_payload_rejects_guest_runner(self):
        with self.assertRaisesRegex(ValidationError, "registered"):
            build_run_payload(
                category_id="category",
                category_type="per-game",
                level_id=None,
                run_date="2024-03-01",
                times={"realtime": "10"},
                allowed_times=["realtime"],
                players=[{"rel": "guest", "id": "runner-1"}],
            )

    def test_runner_list_uses_verified_registered_players(self):
        api = SpeedrunAPI()
        response = Mock(ok=True)
        response.json.return_value = {
            "data": [
                {
                    "players": {
                        "data": [
                            {
                                "id": "runner-1",
                                "rel": "user",
                                "names": {"international": "Runner One"},
                            },
                            {"rel": "guest", "name": "Guest"},
                        ]
                    }
                }
            ]
        }
        api.session.request = Mock(return_value=response)
        self.assertEqual([player["id"] for player in api.game_runners("game")], ["runner-1"])


if __name__ == "__main__":
    unittest.main()
