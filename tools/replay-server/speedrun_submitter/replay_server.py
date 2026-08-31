"""Authenticated HTTP API and dashboard for OpenGOAL replay files."""

from __future__ import annotations

import argparse
import base64
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
import webbrowser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .api import SpeedrunAPIError
from .replay_store import DEFAULT_HOST, DEFAULT_PORT, ReplayStore


MAX_BODY = 16 * 1024 * 1024


DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenGOAL Replay Server</title><style>
:root{color-scheme:dark;--bg:#0b1220;--card:#121d31;--line:#263753;--ink:#e8eef8;--muted:#9cb0ca;--accent:#63d7c8;--bad:#ff8a8a}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#09101c,#10213b);color:var(--ink);font:15px system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:32px 20px}.top{display:flex;justify-content:space-between;align-items:end;gap:16px}h1{margin:0;font-size:30px}p{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:18px;box-shadow:0 18px 50px #0004}button,input,select{font:inherit;color:var(--ink);background:#0c1728;border:1px solid #38506f;border-radius:8px;padding:8px 10px}button{cursor:pointer;background:#183552}button:hover{border-color:var(--accent)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase}.id{font:12px ui-monospace,monospace;color:var(--muted);overflow-wrap:anywhere}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:220px}.replay-group td{padding-top:22px;color:var(--accent);font-size:16px;font-weight:700;border-bottom:2px solid var(--line)}.status-failed{color:var(--bad)}.status-submitted{color:var(--accent)}a{color:var(--accent)}[hidden]{display:none!important}@media(max-width:760px){table,thead,tbody,tr,th,td{display:block}thead{display:none}td{border:0;padding:4px}.replay,.player{padding:14px 0;border-bottom:1px solid var(--line)}.replay-group td{padding-top:22px}}</style></head><body><main>
<section class="card" id="login"><h1>Replay Server Admin</h1><p>Sign in with the administrator username and password. Your login is kept only in this browser tab.</p><form id="login-form" class="row"><input class="grow" name="username" type="text" autocomplete="username" placeholder="Username" required><input class="grow" name="password" type="password" autocomplete="current-password" placeholder="Password" required><button>Open dashboard</button></form><p class="status-failed" id="login-status"></p></section>
<div id="dashboard" hidden><div class="top"><div><h1>OpenGOAL Replay Server</h1><p id="summary">Loading…</p></div><div class="row"><button onclick="refresh()">Refresh</button><button onclick="logout()">Lock</button></div></div>
<section class="card"><h2>Speedrun.com moderator</h2><p>Configure one moderator key on this server. It is stored only in the protected server data and is never returned to the browser or game. Registered runners are loaded from verified runs on the Jak 3 OpenGOAL Missions board, and automatic submissions use the shared YouTube proof video.</p><form id="moderator-form" class="row"><input class="grow" name="api_key" type="password" required placeholder="Moderator API key"><button>Configure & load runners</button></form><div class="row" style="margin-top:12px"><span id="moderator-name">No moderator configured</span><button id="refresh-runners" type="button">Reload runners</button><label><input id="auto-submit" type="checkbox"> Auto-submit mapped-player PBs</label></div><p>Each game installation creates one permanent random Player ID. Map it once below; all existing and future replays from that player inherit the SRC runner.</p><p id="moderator-status"></p></section>
<section class="card"><h2>Players</h2><table><thead><tr><th>Permanent Player ID</th><th>Replays</th><th>SRC runner</th></tr></thead><tbody id="players"></tbody></table></section>
<section class="card"><h2>Replays</h2><table><thead><tr><th>Name</th><th>Category</th><th>Time</th><th>Player ID</th><th>Replay ID</th><th>SRC runner</th><th>Speedrun.com</th><th></th></tr></thead><tbody id="replays"></tbody></table></section></div></main><script>
let state={replays:[],players:[],runners:[],replay_modes:[],moderator:{},settings:{}};
let adminAuthorization=sessionStorage.getItem('replay-admin-authorization')||'';
async function api(path,options={}){options.headers={'Content-Type':'application/json',...(adminAuthorization?{'Authorization':adminAuthorization}:{}),...(options.headers||{})};let r=await fetch(path,options);let data=await r.json().catch(()=>({}));if(!r.ok){if(r.status===401)showLogin(data.error||'Administrator login required');throw Error(data.error||r.statusText)}return data}
function showLogin(message=''){document.querySelector('#login').hidden=false;document.querySelector('#dashboard').hidden=true;document.querySelector('#login-status').textContent=message}
async function connectAdmin(username,password){adminAuthorization='Basic '+btoa(username+':'+password);try{await refresh();sessionStorage.setItem('replay-admin-authorization',adminAuthorization)}catch(x){adminAuthorization='';showLogin(x.message)}}
function logout(){adminAuthorization='';sessionStorage.removeItem('replay-admin-authorization');showLogin('Dashboard locked.')}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function duration(v){let ms=Math.round(v*1000),s=Math.floor(ms/1000),m=Math.floor(s/60);return `${m}:${String(s%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`}
function runnerOptions(selected){return '<option value="">Unassigned</option>'+state.runners.map(r=>`<option value="${r.id}" ${r.id===selected?'selected':''}>${esc(r.display_name)}</option>`).join('')}
function gameLabel(game){return ({jak1:'Jak and Daxter',jak2:'Jak II',jak3:'Jak 3'})[game]||game||'Jak 3'}
function sortedReplays(){return [...state.replays].sort((a,b)=>gameLabel(a.game).localeCompare(gameLabel(b.game))||String(a.category||'').localeCompare(String(b.category||''))||Number(a.time_seconds||0)-Number(b.time_seconds||0)||String(a.display_name||'').localeCompare(String(b.display_name||'')))}
function replayRows(){let previous='';return sortedReplays().map(r=>{let group=`${gameLabel(r.game)} / ${r.category||'Uncategorized'}`;let heading=group===previous?'':`<tr class="replay-group"><td colspan="8">${esc(group)}</td></tr>`;previous=group;return heading+`<tr class="replay"><td><form onsubmit="renameReplay(event,'${r.id}')" class="row"><input class="grow" name="display_name" maxlength="120" value="${esc(r.display_name)}"><button>Rename</button></form></td><td>${esc(r.category)}${r.is_personal_best?' <strong>PB</strong>':' <small>attempt</small>'}${!r.completed?' <strong>UNFINISHED</strong>':''}</td><td>${duration(r.time_seconds)}</td><td class="id">${esc(r.player_id||'Legacy replay')}</td><td class="id">${r.id}</td><td>${esc((state.runners.find(x=>x.id===r.src_runner_id)||{}).display_name||'Unassigned')}</td><td class="status-${r.src_status}">${r.src_run_url?`<a href="${esc(r.src_run_url)}" target="_blank">submitted</a>`:esc(r.src_status)}${r.src_error?`<br><small>${esc(r.src_error)}</small>`:''}</td><td><button onclick="downloadReplay('${r.id}')">Download</button>${r.is_personal_best&&(r.src_status==='failed'||r.src_status==='not_requested')?` <button onclick="submitReplay('${r.id}')" ${r.src_runner_id?'':'disabled'}>Submit</button>`:''}</td></tr>`}).join('')||'<tr><td colspan="8">Complete a run in-game to add the first replay.</td></tr>'}
async function refresh(){state=await api('/api/state');document.querySelector('#login').hidden=true;document.querySelector('#dashboard').hidden=false;document.querySelector('#summary').textContent=`${state.replays.length} replay${state.replays.length===1?'':'s'} · ${state.players.length} player${state.players.length===1?'':'s'} · ${state.runners.length} SRC runner${state.runners.length===1?'':'s'} · ${location.origin}`;document.querySelector('#moderator-name').textContent=state.moderator.display_name?`Moderator: ${state.moderator.display_name}`:'No moderator configured';document.querySelector('#auto-submit').checked=!!state.settings.auto_submit;document.querySelector('#players').innerHTML=state.players.map(p=>`<tr class="player"><td class="id">${esc(p.id)}</td><td>${state.replays.filter(r=>r.player_id===p.id).length}</td><td><select onchange="assignPlayer('${p.id}',this.value)">${runnerOptions(p.src_runner_id||'')}</select></td></tr>`).join('')||'<tr><td colspan="3">No game Player IDs have uploaded a replay yet.</td></tr>';document.querySelector('#replays').innerHTML=replayRows()}
async function renameReplay(e,id){e.preventDefault();await api(`/api/replays/${id}`,{method:'PATCH',body:JSON.stringify({display_name:new FormData(e.target).get('display_name')})});await refresh()}
async function assignPlayer(id,runner){await api(`/api/players/${id}`,{method:'PATCH',body:JSON.stringify({src_runner_id:runner})});await refresh()}
async function submitReplay(id){await api(`/api/replays/${id}/submit`,{method:'POST',body:'{}'});await refresh()}
async function downloadReplay(id){let headers=adminAuthorization?{'Authorization':adminAuthorization}:{};let response=await fetch(`/api/replays/${id}/download`,{headers});if(!response.ok)throw Error('Download failed');let link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());link.download=`replay-${id}.json`;link.click();URL.revokeObjectURL(link.href)}
document.querySelector('#moderator-form').onsubmit=async e=>{e.preventDefault();let out=document.querySelector('#moderator-status');out.textContent='Verifying moderator and loading runners…';try{await api('/api/moderator',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});e.target.reset();out.textContent='Moderator configured.';await refresh()}catch(x){out.textContent=x.message}}
document.querySelector('#refresh-runners').onclick=async()=>{let out=document.querySelector('#moderator-status');out.textContent='Reloading runners…';try{await api('/api/runners/refresh',{method:'POST',body:'{}'});out.textContent='Runner list updated.';await refresh()}catch(x){out.textContent=x.message}}
document.querySelector('#auto-submit').onchange=async e=>{await api('/api/settings',{method:'PATCH',body:JSON.stringify({auto_submit:e.target.checked})});await refresh()};
document.querySelector('#login-form').onsubmit=e=>{e.preventDefault();let data=new FormData(e.target);connectAdmin(data.get('username')||'',data.get('password')||'')};
refresh().catch(()=>{});setInterval(()=>refresh().catch(()=>{}),5000);</script></body></html>"""


class ReplayRequestHandler(BaseHTTPRequestHandler):
    server_version = "OpenGOALReplay/1.0"

    @property
    def store(self) -> ReplayStore:
        return self.server.store  # type: ignore[attr-defined]

    def _authorized(self, role: str) -> bool:
        game_token = self.server.game_token  # type: ignore[attr-defined]
        admin_token = self.server.admin_token  # type: ignore[attr-defined]
        admin_username = self.server.admin_username  # type: ignore[attr-defined]
        admin_password = self.server.admin_password  # type: ignore[attr-defined]
        if not game_token and not admin_token and not admin_username and not admin_password:
            return True
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        basic_username = ""
        basic_password = ""
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                basic_username, separator, basic_password = decoded.partition(":")
                if not separator:
                    basic_username = ""
                    basic_password = ""
            except (ValueError, UnicodeDecodeError):
                pass
        basic_admin = bool(admin_username and admin_password) and (
            secrets.compare_digest(basic_username, admin_username)
            and secrets.compare_digest(basic_password, admin_password)
        )
        token_admin = bool(admin_token) and secrets.compare_digest(supplied, admin_token)
        if role == "admin":
            return basic_admin or token_admin
        return basic_admin or token_admin or (
            bool(game_token) and secrets.compare_digest(supplied, game_token)
        )

    def _require(self, role: str) -> bool:
        if self._authorized(role):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": f"Valid {role} token required"})
        return False

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[replay-server] {self.address_string()} - {format % args}")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json": raise ValueError("Content-Type must be application/json")
        try: size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc: raise ValueError("Invalid Content-Length") from exc
        if size <= 0 or size > MAX_BODY: raise ValueError("Request body is empty or too large")
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict): raise ValueError("JSON body must be an object")
        return data

    def _parts(self) -> list[str]: return [unquote(part) for part in urlparse(self.path).path.split("/") if part]

    def do_GET(self) -> None:
        try:
            parts = self._parts()
            if not parts:
                body = DASHBOARD_HTML.encode("utf-8"); self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
            elif parts == ["health"]:
                self._json(HTTPStatus.OK, {"status": "ok"})
            elif parts == ["api", "state"]:
                if not self._require("game"): return
                query = parse_qs(urlparse(self.path).query)
                player_id = str((query.get("player_id") or [""])[0])
                self._json(HTTPStatus.OK, self.store.public_state(player_id))
            elif len(parts) == 4 and parts[:2] == ["api", "replays"] and parts[3] == "download":
                if not self._require("game"): return
                body = self.store.replay_bytes(parts[2]); self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json"); self.send_header("Content-Disposition", f'attachment; filename="replay-{parts[2]}.json"')
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            else: self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except KeyError: self._json(HTTPStatus.NOT_FOUND, {"error": "Replay not found"})
        except Exception as exc: self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            parts = self._parts()
            role = "game" if parts in (["api", "replays"], ["api", "replay-selection"]) else "admin"
            if not self._require(role): return
            data = self._body()
            if parts == ["api", "replays"]: self._json(HTTPStatus.CREATED, self.store.add_replay(data))
            elif parts == ["api", "replay-selection"]:
                self._json(
                    HTTPStatus.OK,
                    self.store.resolve_replay_selection(
                        str(data.get("category") or ""),
                        str(data.get("player_id") or ""),
                    ),
                )
            elif parts == ["api", "moderator"]: self._json(HTTPStatus.CREATED, self.store.configure_moderator(str(data.get("api_key") or "")))
            elif parts == ["api", "runners", "refresh"]: self._json(HTTPStatus.OK, self.store.refresh_runners())
            elif len(parts) == 4 and parts[:2] == ["api", "replays"] and parts[3] == "submit": self.store.submit_async(parts[2]); self._json(HTTPStatus.ACCEPTED, {"status": "queued"})
            else: self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (ValueError, SpeedrunAPIError) as exc: self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except KeyError: self._json(HTTPStatus.NOT_FOUND, {"error": "Item not found"})
        except Exception as exc: self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_PATCH(self) -> None:
        try:
            parts = self._parts()
            is_player_settings = len(parts) == 3 and parts[:2] == ["api", "player-settings"]
            if not self._require("game" if is_player_settings else "admin"): return
            data = self._body()
            if parts == ["api", "settings"]: self._json(HTTPStatus.OK, self.store.update_settings(data))
            elif is_player_settings:
                self._json(
                    HTTPStatus.OK,
                    self.store.update_player_settings(parts[2], data),
                )
            elif len(parts) == 3 and parts[:2] == ["api", "players"]:
                self._json(
                    HTTPStatus.OK,
                    self.store.assign_player_runner(
                        parts[2], str(data.get("src_runner_id") or "")
                    ),
                )
            elif len(parts) == 3 and parts[:2] == ["api", "replays"]:
                if "display_name" in data:
                    self.store.rename_replay(parts[2], str(data.get("display_name") or ""))
                if "src_runner_id" in data:
                    self.store.assign_replay_runner(parts[2], str(data.get("src_runner_id") or ""))
                self._json(HTTPStatus.OK, next(item for item in self.store.public_state()["replays"] if item["id"] == parts[2]))
            else: self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc: self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except KeyError: self._json(HTTPStatus.NOT_FOUND, {"error": "Item not found"})


class ReplayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: ReplayStore,
        *,
        game_token: str = "",
        admin_token: str = "",
        admin_username: str = "",
        admin_password: str = "",
    ) -> None:
        super().__init__(address, ReplayRequestHandler)
        self.store = store
        self.game_token = game_token
        self.admin_token = admin_token
        self.admin_username = admin_username
        self.admin_password = admin_password


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenGOAL replay server")
    environment_port = os.environ.get("SERVER_PORT") or os.environ.get("REPLAY_PORT")
    try:
        default_port = int(environment_port) if environment_port else DEFAULT_PORT
    except ValueError:
        default_port = DEFAULT_PORT
    parser.add_argument("--host", default=os.environ.get("REPLAY_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    game_token = os.environ.get("REPLAY_GAME_TOKEN", "").strip()
    admin_token = os.environ.get("REPLAY_ADMIN_TOKEN", "").strip()
    admin_username = os.environ.get("REPLAY_ADMIN_USERNAME", "user").strip()
    admin_password = os.environ.get("REPLAY_ADMIN_PASSWORD", "pass")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and (
        not game_token or not ((admin_username and admin_password) or admin_token)
    ):
        parser.error(
            "REPLAY_GAME_TOKEN and admin username/password credentials are required for "
            "non-loopback hosting"
        )
    store = ReplayStore()
    server = ReplayHTTPServer(
        (args.host, args.port),
        store,
        game_token=game_token,
        admin_token=admin_token,
        admin_username=admin_username,
        admin_password=admin_password,
    )
    browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{browser_host}:{args.port}/"
    print(f"OpenGOAL Replay Server is running at {url}")
    print(f"Replay data: {store.root}")
    if not args.no_browser and args.host in {"127.0.0.1", "localhost", "::1"}:
        threading.Timer(0.35, webbrowser.open, args=(url,)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
