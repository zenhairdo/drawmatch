#!/usr/bin/env python3
"""Dependency-free multiplayer beta server for Drawmatch."""

from __future__ import annotations

import json
import os
import random
import secrets
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
ROUND_SECONDS = 90
MAX_BODY_BYTES = 6 * 1024 * 1024
ROOT = Path(__file__).resolve().parent
PROMPTS = (
    "A lighthouse on another planet",
    "The world's worst superhero",
    "Breakfast escaping the kitchen",
    "A tiny city inside a shoe",
    "A dragon working from home",
    "The last tree at the end of time",
    "A museum exhibit from the future",
    "A haunted vending machine",
)


class GameStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.players: dict[str, dict] = {}
        self.matches: dict[str, dict] = {}
        self.artist_queue: deque[str] = deque()
        self.judge_queue: deque[str] = deque()
    def join(self, name: str, role: str) -> dict:
        name = " ".join(name.strip().split())[:20]
        if not name:
            raise ValueError("Enter a player name.")
        if role not in {"artist", "judge"}:
            raise ValueError("Choose artist or judge.")

        with self.lock:
            active_names = {
                player["name"].casefold()
                for player in self.players.values()
                if player["status"] != "left"
            }
            if name.casefold() in active_names:
                raise ValueError("That name is already in use.")

            player_id = secrets.token_urlsafe(18)
            self.players[player_id] = {
                "id": player_id,
                "name": name,
                "role": role,
                "status": "queued",
                "match_id": None,
            }
            self._queue_for(role).append(player_id)
            self._make_matches()
            return {"player_id": player_id}

    def state(self, player_id: str) -> dict:
        with self.lock:
            player = self._player(player_id)
            if player["status"] == "queued":
                queue = self._queue_for(player["role"])
                position = list(queue).index(player_id) + 1
                return {
                    "status": "queued",
                    "role": player["role"],
                    "name": player["name"],
                    "position": position,
                    "artists_waiting": len(self.artist_queue),
                    "judges_waiting": len(self.judge_queue),
                }

            if player["status"] == "left":
                return {"status": "left"}

            match = self.matches[player["match_id"]]
            response = {
                "status": match["status"],
                "role": player["role"],
                "name": player["name"],
                "prompt": match["prompt"],
                "deadline": match["deadline"],
                "submitted": player_id in match["drawings"],
            }

            if player["role"] == "artist":
                response["opponent_submitted"] = any(
                    artist_id != player_id and artist_id in match["drawings"]
                    for artist_id in match["artists"]
                )
            if player["role"] == "judge" and match["status"] in {"judging", "complete"}:
                response["drawings"] = [match["drawings"][artist] for artist in match["artists"]]
            if match["status"] == "complete":
                winner_id = match["winner"]
                response["winner"] = self.players[winner_id]["name"]
                response["artists"] = [self.players[item]["name"] for item in match["artists"]]
                response["drawings"] = [match["drawings"][artist] for artist in match["artists"]]
            return response

    def submit(self, player_id: str, drawing: str) -> None:
        if not drawing.startswith("data:image/png;base64,"):
            raise ValueError("Drawing must be a PNG image.")
        with self.lock:
            player = self._player(player_id)
            if player["role"] != "artist" or not player["match_id"]:
                raise ValueError("Only a matched artist can submit.")
            match = self.matches[player["match_id"]]
            if match["status"] != "drawing":
                raise ValueError("This drawing round is closed.")
            match["drawings"][player_id] = drawing
            if all(artist in match["drawings"] for artist in match["artists"]):
                match["status"] = "judging"

    def vote(self, player_id: str, choice: int) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["role"] != "judge" or not player["match_id"]:
                raise ValueError("Only the match judge can vote.")
            match = self.matches[player["match_id"]]
            if match["status"] != "judging" or choice not in {0, 1}:
                raise ValueError("That vote is not available.")
            match["winner"] = match["artists"][choice]
            match["status"] = "complete"

    def requeue(self, player_id: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["status"] == "queued":
                return
            player["status"] = "queued"
            player["match_id"] = None
            self._queue_for(player["role"]).append(player_id)
            self._make_matches()

    def leave(self, player_id: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["status"] == "queued":
                queue = self._queue_for(player["role"])
                try:
                    queue.remove(player_id)
                except ValueError:
                    pass
            player["status"] = "left"

    def expire_rounds(self) -> None:
        with self.lock:
            now = time.time()
            for match in self.matches.values():
                if match["status"] != "drawing" or now < match["deadline"]:
                    continue
                # Blank canvases keep the match judgeable if an artist times out.
                blank = match.get("blank_drawing")
                for artist in match["artists"]:
                    if artist not in match["drawings"] and blank:
                        match["drawings"][artist] = blank
                if len(match["drawings"]) == 2:
                    match["status"] = "judging"

    def set_blank(self, player_id: str, drawing: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["match_id"]:
                self.matches[player["match_id"]]["blank_drawing"] = drawing

    def _make_matches(self) -> None:
        while len(self.artist_queue) >= 2 and self.judge_queue:
            artists = [self.artist_queue.popleft(), self.artist_queue.popleft()]
            judge = self.judge_queue.popleft()
            match_id = secrets.token_urlsafe(12)
            self.matches[match_id] = {
                "id": match_id,
                "artists": artists,
                "judge": judge,
                "prompt": random.choice(PROMPTS),
                "deadline": time.time() + ROUND_SECONDS,
                "status": "drawing",
                "drawings": {},
                "winner": None,
                "blank_drawing": None,
            }
            for player_id in [*artists, judge]:
                self.players[player_id]["status"] = "matched"
                self.players[player_id]["match_id"] = match_id

    def _queue_for(self, role: str) -> deque[str]:
        return self.artist_queue if role == "artist" else self.judge_queue

    def _player(self, player_id: str) -> dict:
        try:
            return self.players[player_id]
        except KeyError as error:
            raise ValueError("Player session not found.") from error


STORE = GameStore()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            STORE.expire_rounds()
            player_id = parse_qs(parsed.query).get("player_id", [""])[0]
            self._handle(lambda: STORE.state(player_id))
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        routes = {
            "/api/join": self._join,
            "/api/submit": self._submit,
            "/api/vote": self._vote,
            "/api/requeue": self._requeue,
            "/api/leave": self._leave,
            "/api/blank": self._blank,
        }
        action = routes.get(urlparse(self.path).path)
        if action is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._handle(action)

    def _join(self) -> dict:
        body = self._body()
        return STORE.join(body.get("name", ""), body.get("role", ""))

    def _submit(self) -> dict:
        body = self._body()
        STORE.submit(body.get("player_id", ""), body.get("drawing", ""))
        return {"ok": True}

    def _vote(self) -> dict:
        body = self._body()
        STORE.vote(body.get("player_id", ""), body.get("choice"))
        return {"ok": True}

    def _requeue(self) -> dict:
        STORE.requeue(self._body().get("player_id", ""))
        return {"ok": True}

    def _leave(self) -> dict:
        STORE.leave(self._body().get("player_id", ""))
        return {"ok": True}

    def _blank(self) -> dict:
        body = self._body()
        STORE.set_blank(body.get("player_id", ""), body.get("drawing", ""))
        return {"ok": True}

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_BYTES:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def _handle(self, action) -> None:
        try:
            payload = action()
            self._json(HTTPStatus.OK, payload)
        except (ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Server error."})

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Drawmatch beta running at http://localhost:{PORT}")
    print(f"LAN players can connect to this computer's IP on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
