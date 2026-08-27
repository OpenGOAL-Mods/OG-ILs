# OpenGOAL Replay Server

Double-click `RUN_REPLAY_SERVER.bat` before starting Jak 3. It creates a private Python environment on first launch, starts the loopback-only server at <http://127.0.0.1:7878/>, and opens the dashboard.

The game sends every completed IL replay with two permanent identifiers:

- The replay ID uniquely identifies that run.
- The player ID is generated once per game installation and reused for every upload.

In the dashboard, configure one Jak 3 OpenGOAL Missions moderator API key, reload the existing leaderboard runners, then map each player ID to its real Speedrun.com runner. The mapping applies to existing unsubmitted replays and future uploads. With automatic submission enabled, a newly mapped player's newest pending PB for each mission is submitted using the shared proof video.

Replay data and the moderator key are stored under `%APPDATA%\OpenGOAL\jak3\replay-server` by default. The API key is never returned to the browser or game. Set `OPENGOAL_REPLAY_DATA` before launch to select a different data directory.

Run the tests from this folder with:

```powershell
python -m unittest discover -s tests
```
