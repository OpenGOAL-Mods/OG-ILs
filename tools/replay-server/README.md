# OpenGOAL Replay Server

Double-click `RUN_REPLAY_SERVER.bat` before starting Jak 3. It creates a private Python environment on first launch, starts the loopback-only server at <http://127.0.0.1:7878/>, and opens the dashboard.

The game sends every completed IL replay with two permanent identifiers:

- The replay ID uniquely identifies that run.
- The player ID is generated once per game installation and reused for every upload.

In the dashboard, configure one Jak 3 OpenGOAL Missions moderator API key, reload the existing leaderboard runners, then map each player ID to its real Speedrun.com runner. The mapping applies to existing unsubmitted replays and future uploads. With automatic submission enabled, a newly mapped player's newest pending PB for each mission is submitted using the shared proof video.

## Ghost modes

The mode list is supplied by the server with stable IDs, labels, and descriptions so new resolution strategies can be added without changing the replay file format or ghost loader.

- **Default - Next Place:** without a personal time, race the slowest completed server replay. Once you have a PB, race the closest strictly faster replay.
- **Default - Next 3 Places:** without a personal time, race the three slowest completed server replays. Once you have a PB, race the three closest strictly faster replays.
- **Race vs Your Best:** race your fastest completed replay for the mission.
- **Race vs WR:** race the fastest completed replay available on the server.
- **Race vs Last Attempt:** race your newest attempt, including a retry that was reset before completion.
- **Custom:** race any combination of selected mission replays.

Interrupted attempts are uploaded when the next retry starts. They are never treated as PBs and never submitted to Speedrun.com.

Replay data and the moderator key are stored under `%APPDATA%\OpenGOAL\jak3\replay-server` by default. The API key is never returned to the browser or game. Set `OPENGOAL_REPLAY_DATA` before launch to select a different data directory.

Run the tests from this folder with:

```powershell
python -m unittest discover -s tests
```
