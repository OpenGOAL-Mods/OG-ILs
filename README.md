# Jak II / 3 - All Missions Speedrun Mod (OpenGOAL)

- Play now via the JakMods mod list! https://jakmods.dev
- Jak II
  - [Speedrun.com Leaderboard](https://www.speedrun.com/jak2og_missions)
  - [Points Leaderboard](http://136.0.251.17:25817/?mode=jak2)
- Jak 3
  - [Speedrun.com Leaderboard](https://www.speedrun.com/jak3og_missions)
  - [Points Leaderboard](http://136.0.251.17:25817/?mode=jak3)

Adds Individual Level categories to the speedrunner menu (hold `L1`+`R1` and press `Start` or `Select`)
<img src="https://github.com/user-attachments/assets/2719a96a-2eae-4912-8773-28beea359423" width="800"/>

Adds in-game timer and end screen after completing each mission, with some statistics (more to come)
![image](https://github.com/user-attachments/assets/00ac1184-8ca4-4e15-9309-4e3eaf6d8550)

## Replay racing and submissions

Jak 3 uploads completed mission replays to the local replay server. Start it by double-clicking [`tools/replay-server/RUN_REPLAY_SERVER.bat`](tools/replay-server/RUN_REPLAY_SERVER.bat), then open <http://127.0.0.1:7878/>.

Each game installation keeps one permanent random player ID and sends it with every replay. The dashboard lets an administrator map that ID to an existing Jak 3 OpenGOAL Missions runner using one server-side moderator API key. New mapped-player PBs can then be submitted automatically. See [`tools/replay-server/README.md`](tools/replay-server/README.md) for setup and testing details.

Replay racing supports an adaptive next-place mode, personal best, fastest available server replay, last attempt (including unfinished retries), and multi-replay custom races.
