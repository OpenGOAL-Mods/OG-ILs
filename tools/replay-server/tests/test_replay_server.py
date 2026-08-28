import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from speedrun_submitter.replay_server import ReplayHTTPServer
from speedrun_submitter.replay_store import PROOF_VIDEO_URL, ReplayStore


def replay_envelope():
    return {"category": "wascity-bbush-get-to-18", "time_seconds": 8.125,
            "player_id": "player-0123456789abcdef",
            "is_personal_best": True,
            "src_level_id": "9203p03d", "src_category_id": "rkl7n8qd",
            "src_variable_labels": {"Wasteland Vehicle": "N/A"},
            "replay": {"version": 1, "category": "wascity-bbush-get-to-18",
                       "mod_version": "v0.1.13", "count": 1, "frames": [[0, 1, 2, 3]]}}


class FakeAPI:
    submitted = []
    def __init__(self, api_key): self.api_key = api_key
    def profile(self): return {"id": "mod-1", "names": {"international": "Moderator"}}
    def game_details(self, game_id): return {"moderators": {"mod-1": "moderator"}}
    def game_runners(self, game_id):
        return [
            {"id": "runner-1", "rel": "user", "names": {"international": "Runner One"}},
            {"id": "runner-2", "rel": "user", "names": {"international": "Runner Two"}},
        ]
    def load_game_form(self, game_id):
        return {"game": {"ruleset": {"run-times": ["realtime", "ingame"],
                                      "default-time": "ingame", "emulators-allowed": True}},
                "platforms": [{"id": "other"}, {"id": "8gej2n93"}], "variables": []}
    def submit_run(self, payload):
        self.submitted.append(payload); return {"id": "run-1", "weblink": "https://www.speedrun.com/run/run-1"}


class ReplayStoreTests(unittest.TestCase):
    def setUp(self):
        FakeAPI.submitted = []; self.tmp = TemporaryDirectory()
        self.store = ReplayStore(Path(self.tmp.name), api_factory=FakeAPI)
    def tearDown(self): self.tmp.cleanup()
    def test_replays_get_random_permanent_ids_and_can_be_renamed(self):
        first, second = self.store.add_replay(replay_envelope()), self.store.add_replay(replay_envelope())
        self.assertEqual(len(first["id"]), 32); self.assertNotEqual(first["id"], second["id"])
        self.store.rename_replay(first["id"], "Orb 18 PB")
        self.assertEqual(json.loads(self.store.replay_bytes(first["id"]))["count"], 1)
        reloaded = ReplayStore(Path(self.tmp.name), api_factory=FakeAPI)
        saved = next(item for item in reloaded.public_state()["replays"] if item["id"] == first["id"])
        self.assertEqual(saved["display_name"], "Orb 18 PB")

    def test_upload_requires_a_well_formed_player_id(self):
        missing = replay_envelope()
        del missing["player_id"]
        with self.assertRaisesRegex(ValueError, "player_id"):
            self.store.add_replay(missing)

        invalid = replay_envelope()
        invalid["player_id"] = "not allowed!"
        with self.assertRaisesRegex(ValueError, "player_id is invalid"):
            self.store.add_replay(invalid)

    def test_distinct_player_ids_create_distinct_permanent_players(self):
        first = self.store.add_replay(replay_envelope())
        other = replay_envelope()
        other["player_id"] = "fedcba98765432100123456789abcdef"
        second = self.store.add_replay(other)
        self.assertNotEqual(first["player_id"], second["player_id"])
        self.assertEqual(len(self.store.public_state()["players"]), 2)

    def test_multiple_replays_can_be_selected_and_old_setting_is_migrated(self):
        first = self.store.add_replay(replay_envelope())
        second = self.store.add_replay(replay_envelope())
        settings = self.store.update_settings(
            {"selected_replay_ids": [first["id"], second["id"], first["id"]]}
        )
        self.assertEqual(settings["selected_replay_ids"], [first["id"], second["id"]])

        legacy = self.store.public_state()
        legacy["settings"] = {
            "selected_replay_id": first["id"],
            "selected_runner_id": "",
            "auto_submit": True,
        }
        self.store.index_path.write_text(json.dumps(legacy), encoding="utf-8")
        migrated = ReplayStore(Path(self.tmp.name), api_factory=FakeAPI)
        self.assertEqual(migrated.public_state()["settings"]["selected_replay_ids"], [first["id"]])

    def add_timed_replay(self, seconds, player_id, *, completed=True):
        envelope = replay_envelope()
        envelope["time_seconds"] = seconds
        envelope["player_id"] = player_id
        envelope["completed"] = completed
        envelope["is_personal_best"] = completed
        return self.store.add_replay(envelope)

    def test_default_mode_steps_from_slowest_to_next_faster_replay(self):
        slowest = self.add_timed_replay(60, "slow-player-0000000001")
        next_faster = self.add_timed_replay(50, "fast-player-0000000001")
        self.add_timed_replay(40, "wr-player-000000000001")

        no_time = self.store.resolve_replay_selection(
            slowest["category"], "new-player-00000000001"
        )
        self.assertEqual(no_time["replays"][0]["id"], slowest["id"])

        self.add_timed_replay(55, "new-player-00000000001")
        improving = self.store.resolve_replay_selection(
            slowest["category"], "new-player-00000000001"
        )
        self.assertEqual(improving["replays"][0]["id"], next_faster["id"])

    def test_next_three_mode_steps_through_three_faster_replays(self):
        slowest = self.add_timed_replay(70, "slow-player-0000000001")
        second_slowest = self.add_timed_replay(60, "slow-player-0000000002")
        third_slowest = self.add_timed_replay(50, "slow-player-0000000003")
        nearest = self.add_timed_replay(40, "fast-player-0000000001")
        middle = self.add_timed_replay(30, "fast-player-0000000002")
        fastest = self.add_timed_replay(20, "wr-player-000000000001")
        category = slowest["category"]
        player_id = "new-player-00000000001"
        self.store.update_settings({"replay_mode": "next_three"})

        no_time = self.store.resolve_replay_selection(category, player_id)
        self.assertEqual(
            [item["id"] for item in no_time["replays"]],
            [slowest["id"], second_slowest["id"], third_slowest["id"]],
        )

        self.add_timed_replay(45, player_id)
        improving = self.store.resolve_replay_selection(category, player_id)
        self.assertEqual(
            [item["id"] for item in improving["replays"]],
            [nearest["id"], middle["id"], fastest["id"]],
        )

    def test_named_modes_resolve_pb_wr_last_attempt_and_custom(self):
        player_id = "mode-player-000000000001"
        pb = self.add_timed_replay(45, player_id)
        wr = self.add_timed_replay(30, "wr-player-000000000001")
        unfinished = self.add_timed_replay(12, player_id, completed=False)
        category = pb["category"]

        self.store.update_settings({"replay_mode": "personal_best"})
        self.assertEqual(
            self.store.resolve_replay_selection(category, player_id)["replays"][0]["id"],
            pb["id"],
        )
        self.store.update_settings({"replay_mode": "world_record"})
        self.assertEqual(
            self.store.resolve_replay_selection(category, player_id)["replays"][0]["id"],
            wr["id"],
        )
        self.store.update_settings({"replay_mode": "last_attempt"})
        self.assertEqual(
            self.store.resolve_replay_selection(category, player_id)["replays"][0]["id"],
            unfinished["id"],
        )
        self.assertFalse(unfinished["completed"])
        self.assertFalse(unfinished["is_personal_best"])

        self.store.update_settings(
            {"replay_mode": "custom", "selected_replay_ids": [wr["id"], pb["id"]]}
        )
        custom = self.store.resolve_replay_selection(category, player_id)
        self.assertEqual([item["id"] for item in custom["replays"]], [wr["id"], pb["id"]])

    def test_rejects_unknown_replay_mode(self):
        with self.assertRaisesRegex(ValueError, "replay_mode is invalid"):
            self.store.update_settings({"replay_mode": "future-typo"})

    def test_each_game_installation_has_independent_ghost_settings(self):
        first = self.add_timed_replay(50, "settings-player-0000001")
        second = self.add_timed_replay(40, "settings-player-0000002")
        self.store.update_player_settings(
            first["player_id"],
            {"replay_mode": "custom", "selected_replay_ids": [first["id"]]},
        )
        self.store.update_player_settings(
            second["player_id"], {"replay_mode": "world_record"}
        )

        first_state = self.store.public_state(first["player_id"])
        second_state = self.store.public_state(second["player_id"])
        self.assertEqual(first_state["settings"]["replay_mode"], "custom")
        self.assertEqual(first_state["settings"]["selected_replay_ids"], [first["id"]])
        self.assertEqual(second_state["settings"]["replay_mode"], "world_record")
        self.assertNotIn("player_settings", first_state)
        self.assertEqual(
            self.store.resolve_replay_selection(
                first["category"], first["player_id"]
            )["replays"][0]["id"],
            first["id"],
        )
        self.assertEqual(
            self.store.resolve_replay_selection(
                second["category"], second["player_id"]
            )["replays"][0]["id"],
            second["id"],
        )
    def test_player_mapping_auto_submits_with_moderator_key(self):
        moderator = self.store.configure_moderator("secret")
        self.assertEqual(moderator["user_id"], "mod-1")
        replay = self.store.add_replay(replay_envelope())
        self.assertEqual(replay["src_runner_id"], "")
        self.store.assign_player_runner(replay["player_id"], "runner-1")
        deadline = time.time() + 2
        while time.time() < deadline:
            current = next(r for r in self.store.public_state()["replays"] if r["id"] == replay["id"])
            if current["src_status"] == "submitted": break
            time.sleep(0.01)
        self.assertEqual(current["src_status"], "submitted")
        self.assertEqual(FakeAPI.submitted[0]["run"]["level"], "9203p03d")
        self.assertEqual(FakeAPI.submitted[0]["run"]["category"], "rkl7n8qd")
        self.assertEqual(FakeAPI.submitted[0]["run"]["video"], PROOF_VIDEO_URL)
        self.assertEqual(FakeAPI.submitted[0]["run"]["times"]["ingame"], 8.125)
        self.assertEqual(FakeAPI.submitted[0]["run"]["platform"], "8gej2n93")
        self.assertEqual(
            FakeAPI.submitted[0]["run"]["players"],
            [{"rel": "user", "id": "runner-1"}],
        )

    def test_player_mapping_is_permanent_and_applies_to_future_replays(self):
        self.store.configure_moderator("secret")
        replay = self.store.add_replay(replay_envelope())
        self.store.update_settings({"auto_submit": False})
        self.store.assign_player_runner(replay["player_id"], "runner-2")
        future = self.store.add_replay(replay_envelope())
        self.assertEqual(future["player_id"], replay["player_id"])
        self.assertEqual(future["src_runner_id"], "runner-2")
        reloaded = ReplayStore(Path(self.tmp.name), api_factory=FakeAPI)
        player = next(item for item in reloaded.public_state()["players"]
                      if item["id"] == replay["player_id"])
        self.assertEqual(player["src_runner_id"], "runner-2")

    def test_admin_can_map_replay_id_to_runner_before_submission(self):
        self.store.configure_moderator("secret")
        replay = self.store.add_replay(replay_envelope())
        mapped = self.store.assign_replay_runner(replay["id"], "runner-2")
        self.assertEqual(mapped["src_runner_id"], "runner-2")
        reloaded = ReplayStore(Path(self.tmp.name), api_factory=FakeAPI)
        saved = next(
            item for item in reloaded.public_state()["replays"] if item["id"] == replay["id"]
        )
        self.assertEqual(saved["src_runner_id"], "runner-2")
    def test_public_state_never_exposes_api_keys(self):
        self.store.configure_moderator("secret")
        state = self.store.public_state()
        self.assertNotIn("api_key", state["moderator"])
        self.assertEqual(state["runners"][0]["display_name"], "Runner One")

    def test_rejects_key_that_is_not_a_board_moderator(self):
        class NonModeratorAPI(FakeAPI):
            def game_details(self, game_id): return {"moderators": {}}

        store = ReplayStore(Path(self.tmp.name) / "non-moderator", api_factory=NonModeratorAPI)
        with self.assertRaisesRegex(Exception, "not a moderator"):
            store.configure_moderator("regular-user-key")

    def test_manual_submission_requires_configuration(self):
        replay = self.store.add_replay(replay_envelope())
        with self.assertRaisesRegex(ValueError, "moderator API key"):
            self.store.submit_async(replay["id"])

    def test_non_pb_replay_is_stored_but_never_auto_submitted(self):
        self.store.configure_moderator("secret")
        self.store.update_settings({"selected_runner_id": "runner-1", "auto_submit": True})
        envelope = replay_envelope()
        envelope["is_personal_best"] = False
        replay = self.store.add_replay(envelope)
        self.assertFalse(replay["is_personal_best"])
        self.assertEqual(replay["src_status"], "not_requested")
        self.assertEqual(FakeAPI.submitted, [])
        with self.assertRaisesRegex(ValueError, "Only personal-best"):
            self.store.submit_async(replay["id"])


class ReplayHTTPTests(unittest.TestCase):
    def test_state_upload_and_download_over_loopback(self):
        with TemporaryDirectory() as temporary:
            store = ReplayStore(Path(temporary), api_factory=FakeAPI)
            server = ReplayHTTPServer(("127.0.0.1", 0), store)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base, timeout=2) as response:
                    dashboard = response.read().decode("utf-8")
                    self.assertIn("Speedrun.com moderator", dashboard)
                    self.assertIn("Ghost mode", dashboard)
                    self.assertIn("Replay Server Admin", dashboard)
                    self.assertIn('id="players"', dashboard)
                    self.assertIn("assignPlayer", dashboard)
                with urlopen(f"{base}/api/state", timeout=2) as response:
                    self.assertEqual(json.load(response)["replays"], [])
                body = json.dumps(replay_envelope()).encode("utf-8")
                request = Request(
                    f"{base}/api/replays", data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=2) as response:
                    replay_id = json.load(response)["id"]
                selection = Request(
                    f"{base}/api/replay-selection",
                    data=json.dumps({
                        "category": "wascity-bbush-get-to-18",
                        "player_id": "player-0123456789abcdef",
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(selection, timeout=2) as response:
                    self.assertEqual(json.load(response)["replays"][0]["id"], replay_id)
                store.configure_moderator("secret")
                mapping = Request(
                    f"{base}/api/players/player-0123456789abcdef",
                    data=json.dumps({"src_runner_id": "runner-2"}).encode("utf-8"),
                    method="PATCH",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(mapping, timeout=2) as response:
                    self.assertEqual(json.load(response)["src_runner_id"], "runner-2")
                with urlopen(f"{base}/api/replays/{replay_id}/download", timeout=2) as response:
                    self.assertEqual(json.load(response)["count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_hosted_server_separates_game_and_admin_access(self):
        with TemporaryDirectory() as temporary:
            store = ReplayStore(Path(temporary), api_factory=FakeAPI)
            server = ReplayHTTPServer(
                ("127.0.0.1", 0),
                store,
                game_token="game-secret",
                admin_token="admin-secret",
            )
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/health", timeout=2) as response:
                    self.assertEqual(json.load(response)["status"], "ok")
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(f"{base}/api/state", timeout=2)
                self.assertEqual(unauthorized.exception.code, 401)
                unauthorized.exception.close()

                game_state = Request(
                    f"{base}/api/state?player_id=player-0123456789abcdef",
                    headers={"Authorization": "Bearer game-secret"},
                )
                with urlopen(game_state, timeout=2) as response:
                    self.assertEqual(json.load(response)["replays"], [])

                game_settings = Request(
                    f"{base}/api/player-settings/player-0123456789abcdef",
                    data=json.dumps({"replay_mode": "next_three"}).encode("utf-8"),
                    method="PATCH",
                    headers={
                        "Authorization": "Bearer game-secret",
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(game_settings, timeout=2) as response:
                    self.assertEqual(json.load(response)["replay_mode"], "next_three")

                admin_settings = Request(
                    f"{base}/api/settings",
                    data=json.dumps({"auto_submit": False}).encode("utf-8"),
                    method="PATCH",
                    headers={
                        "Authorization": "Bearer game-secret",
                        "Content-Type": "application/json",
                    },
                )
                with self.assertRaises(HTTPError) as forbidden_game_token:
                    urlopen(admin_settings, timeout=2)
                self.assertEqual(forbidden_game_token.exception.code, 401)
                forbidden_game_token.exception.close()

                admin_settings.add_header("Authorization", "Bearer admin-secret")
                with urlopen(admin_settings, timeout=2) as response:
                    self.assertFalse(json.load(response)["auto_submit"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == "__main__": unittest.main()
