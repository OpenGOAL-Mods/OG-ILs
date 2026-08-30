# SparkedHost deployment

This service is small enough to begin at **10% CPU, 300 MB RAM, and 1 GB persistent disk**. Increase a limit only after the two-player test shows sustained pressure, restarts, or storage growth.

## Service settings

- Runtime: Python 3
- Entry file: `tools/replay-server/app.py` when the full repository is checked out, or `app.py` when this folder is uploaded by itself
- Python packages: `requests>=2.31,<3`
- Network: one public web allocation with its assigned primary port
- Health check: `GET /health`
- Persistent data directory: `/home/container/data`

The service reads its assigned port from `SERVER_PORT`. Configure these environment variables in the hosting panel:

```text
REPLAY_HOST=0.0.0.0
SERVER_PORT=<the assigned primary port>
OPENGOAL_REPLAY_DATA=/home/container/data
REPLAY_GAME_TOKEN=<a long random secret shared with game clients>
REPLAY_ADMIN_USERNAME=user
REPLAY_ADMIN_PASSWORD=pass
```

The game token is mandatory when the server listens publicly. Give testers only that token. The dashboard defaults to username `user` and password `pass`; the two admin environment variables make those credentials explicit and allow them to be changed later without editing the server. Never enter the admin password in-game.

## First validation

1. Open `https://<public-host>/health` and confirm it returns `{"status":"ok"}`.
2. Open the public host root, sign in as `user` with password `pass`, and confirm the dashboard loads.
3. In each game, open **Replay Server → Server connection**, enter the public HTTPS URL and game token, then save and refresh.
4. Upload one replay from each tester. Confirm both permanent Player IDs appear separately in the dashboard.
5. Map each Player ID to the correct Speedrun.com runner before enabling automatic PB submission.

During the test, check RAM, CPU, restarts, response time, and disk growth in Apollo Panel. The replay JSON files are small, so 1 GB should hold many test runs. Back up `/home/container/data` before moving or rebuilding the service.

SparkedHost's Python web-server guide documents binding to `0.0.0.0` and the service's primary port: <https://help.sparkedhost.com/en/article/how-to-host-a-flask-web-server-on-discord-bot-hosting-1rgatut/>.
