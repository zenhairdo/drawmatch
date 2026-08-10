#!/usr/bin/env python3
"""Dependency-free multiplayer beta server for Drawmatch."""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import string
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
SPEEDPAINT_FADE_SECONDS = 3
ONLINE_TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 6 * 1024 * 1024
ROOT = Path(__file__).resolve().parent
YOUTUBE_TRACKS = tuple(
    item.strip()
    for item in os.environ.get("YOUTUBE_TRACKS", "").split(",")
    if item.strip()
)
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
        self.rooms: dict[str, dict] = {}
        self.queues = {
            (mode, artist_count, role): deque()
            for mode in ("prompted", "promptless", "speedpaint")
            for artist_count in (2, 4)
            if mode != "speedpaint" or artist_count == 2
            for role in ("artist", "judge")
        }
    def join(self, name: str, role: str, mode: str, artist_count: int) -> dict:
        name = self._clean_name(name)
        if role not in {"artist", "judge"}:
            raise ValueError("Choose artist or judge.")
        self._validate_role_mode(role, mode, artist_count)

        with self.lock:
            active_names = {
                player["name"].casefold()
                for player in self.players.values()
                if player["status"] != "left"
            }
            if name.casefold() in active_names:
                raise ValueError("That username is already queued or playing.")

            player_id = secrets.token_urlsafe(18)
            self.players[player_id] = {
                "id": player_id,
                "name": name,
                "picture": "",
                "role": role,
                "mode": mode,
                "artist_count": artist_count,
                "status": "queued",
                "match_id": None,
                "last_seen": time.time(),
            }
            self._queue_for(role, mode, artist_count).append(player_id)
            self._make_matches()
            return {"player_id": player_id}

    def create_room(
        self, name: str, role: str, mode: str, artist_count: int
    ) -> dict:
        with self.lock:
            self._validate_role_mode(role, mode, artist_count)
            code = self._new_room_code()
            self.rooms[code] = {
                "code": code,
                "mode": mode,
                "artist_count": artist_count,
                "status": "waiting",
                "artists": [],
                "judge": None,
            }
            player_id = self._add_room_player(name, self.rooms[code], role)
            return {"player_id": player_id, "room_code": code}

    def join_room(self, name: str, code: str, role: str) -> dict:
        with self.lock:
            code = code.strip().upper()
            room = self.rooms.get(code)
            if room is None or room["status"] != "waiting":
                raise ValueError("Private room not found or already playing.")
            player_id = self._add_room_player(name, room, role)
            return {"player_id": player_id, "room_code": code}

    def state(self, player_id: str) -> dict:
        with self.lock:
            player = self._player(player_id)
            player["last_seen"] = time.time()
            if player["status"] == "queued":
                queue = self._queue_for(
                    player["role"], player["mode"], player["artist_count"]
                )
                position = list(queue).index(player_id) + 1
                return {
                    "status": "queued",
                    "role": player["role"],
                    "mode": player["mode"],
                    "artist_count": player["artist_count"],
                    "name": player["name"],
                    "picture": player["picture"],
                    "position": position,
                    "artists_waiting": len(
                        self._queue_for("artist", player["mode"], player["artist_count"])
                    ),
                    "judges_waiting": len(
                        self._queue_for("judge", player["mode"], player["artist_count"])
                    ),
                }

            if player["status"] == "room":
                room = self.rooms[player["room_code"]]
                return {
                    "status": "room",
                    "role": player["role"],
                    "mode": player["mode"],
                    "artist_count": room["artist_count"],
                    "name": player["name"],
                    "picture": player["picture"],
                    "room_code": room["code"],
                    "artists_waiting": len(room["artists"]),
                    "judges_waiting": 1 if room["judge"] else 0,
                }

            if player["status"] == "left":
                return {"status": "left"}

            match = self.matches[player["match_id"]]
            response = {
                "status": match["status"],
                "role": player["role"],
                "mode": match["mode"],
                "artist_count": match["artist_count"],
                "name": player["name"],
                "picture": player["picture"],
                "prompt": match["prompt"],
                "deadline": match["deadline"],
                "submitted": player_id in match["drawings"],
            }
            if match["mode"] == "speedpaint":
                response.update(
                    fade_seconds=SPEEDPAINT_FADE_SECONDS,
                    music_video_id=match["music_video_id"],
                    round_started_at=match["round_started_at"],
                )
                if player["role"] == "judge" and match["status"] == "song_select":
                    response["music_choices"] = list(YOUTUBE_TRACKS)

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

    def stats(self) -> dict:
        with self.lock:
            cutoff = time.time() - ONLINE_TIMEOUT_SECONDS
            online = sum(
                player["status"] != "left" and player.get("last_seen", 0) >= cutoff
                for player in self.players.values()
            )
            return {"online": online}

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

    def select_music(self, player_id: str, song: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["role"] != "judge" or not player["match_id"]:
                raise ValueError("Only the match judge can choose the song.")
            match = self.matches[player["match_id"]]
            if match["mode"] != "speedpaint" or match["status"] != "song_select":
                raise ValueError("This match is not waiting for a song.")
            video_id = self._youtube_video_id(song)
            started_at = time.time()
            match.update(
                music_video_id=video_id,
                round_started_at=started_at,
                deadline=started_at + ROUND_SECONDS,
                status="drawing",
            )

    def vote(self, player_id: str, choice: int) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["role"] != "judge" or not player["match_id"]:
                raise ValueError("Only the match judge can vote.")
            match = self.matches[player["match_id"]]
            if match["status"] != "judging" or choice not in range(len(match["artists"])):
                raise ValueError("That vote is not available.")
            match["winner"] = match["artists"][choice]
            match["status"] = "complete"

    def requeue(self, player_id: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["status"] == "queued":
                return
            previous_match = self.matches.get(player.get("match_id"))
            player["status"] = "queued"
            player["match_id"] = None
            if previous_match and previous_match.get("room_code"):
                room = self.rooms[previous_match["room_code"]]
                if room["status"] != "waiting":
                    room.update(status="waiting", artists=[], judge=None)
                player["status"] = "room"
                player["room_code"] = room["code"]
                self._seat_player(room, player_id, player["role"])
                self._start_room_if_ready(room)
            else:
                self._queue_for(
                    player["role"], player["mode"], player["artist_count"]
                ).append(player_id)
                self._make_matches()

    def leave(self, player_id: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["status"] == "queued":
                queue = self._queue_for(
                    player["role"], player["mode"], player["artist_count"]
                )
                try:
                    queue.remove(player_id)
                except ValueError:
                    pass
            elif player["status"] == "room":
                room = self.rooms.get(player["room_code"])
                if room:
                    self._unseat_player(room, player_id)
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
                if len(match["drawings"]) == len(match["artists"]):
                    match["status"] = "judging"

    def set_blank(self, player_id: str, drawing: str) -> None:
        with self.lock:
            player = self._player(player_id)
            if player["match_id"]:
                self.matches[player["match_id"]]["blank_drawing"] = drawing

    def _make_matches(self) -> None:
        for mode in ("prompted", "promptless", "speedpaint"):
            for artist_count in ((2,) if mode == "speedpaint" else (2, 4)):
                artists_queue = self._queue_for("artist", mode, artist_count)
                judges_queue = self._queue_for("judge", mode, artist_count)
                while len(artists_queue) >= artist_count and judges_queue:
                    artists = [artists_queue.popleft() for _ in range(artist_count)]
                    judge = judges_queue.popleft()
                    match_id = secrets.token_urlsafe(12)
                    speedpaint = mode == "speedpaint"
                    started_at = None if speedpaint else time.time()
                    self.matches[match_id] = {
                        "id": match_id,
                        "artists": artists,
                        "artist_count": artist_count,
                        "judge": judge,
                        "mode": mode,
                        "prompt": random.choice(PROMPTS) if mode != "promptless" else "",
                        "round_started_at": started_at,
                        "deadline": None if speedpaint else started_at + ROUND_SECONDS,
                        "music_video_id": "",
                        "status": "song_select" if speedpaint else "drawing",
                        "drawings": {},
                        "winner": None,
                        "blank_drawing": None,
                    }
                    for player_id in [*artists, judge]:
                        self.players[player_id]["status"] = "matched"
                        self.players[player_id]["match_id"] = match_id

    def _add_room_player(self, name: str, room: dict, role: str) -> str:
        if role not in {"artist", "judge"}:
            raise ValueError("Choose artist or judge.")
        name = self._clean_name(name)
        if any(
            player["name"].casefold() == name.casefold() and player["status"] != "left"
            for player in self.players.values()
        ):
            raise ValueError("That username is already queued or playing.")
        if role == "artist" and len(room["artists"]) >= room["artist_count"]:
            raise ValueError("This room already has all of its artists.")
        if role == "judge" and room["judge"] is not None:
            raise ValueError("This room already has a judge.")

        player_id = secrets.token_urlsafe(18)
        self.players[player_id] = {
            "id": player_id,
            "name": name,
            "picture": "",
            "role": role,
            "mode": room["mode"],
            "artist_count": room["artist_count"],
            "status": "room",
            "room_code": room["code"],
            "match_id": None,
            "last_seen": time.time(),
        }
        self._seat_player(room, player_id, role)
        self._start_room_if_ready(room)
        return player_id

    @staticmethod
    def _seat_player(room: dict, player_id: str, role: str) -> None:
        if role == "artist":
            room["artists"].append(player_id)
        else:
            room["judge"] = player_id

    @staticmethod
    def _unseat_player(room: dict, player_id: str) -> None:
        if player_id in room["artists"]:
            room["artists"].remove(player_id)
        elif room["judge"] == player_id:
            room["judge"] = None

    def _start_room_if_ready(self, room: dict) -> None:
        if len(room["artists"]) != room["artist_count"] or room["judge"] is None:
            return
        match_id = secrets.token_urlsafe(12)
        speedpaint = room["mode"] == "speedpaint"
        started_at = None if speedpaint else time.time()
        self.matches[match_id] = {
            "id": match_id,
            "artists": list(room["artists"]),
            "artist_count": room["artist_count"],
            "judge": room["judge"],
            "mode": room["mode"],
            "prompt": random.choice(PROMPTS) if room["mode"] != "promptless" else "",
            "round_started_at": started_at,
            "deadline": None if speedpaint else started_at + ROUND_SECONDS,
            "music_video_id": "",
            "status": "song_select" if speedpaint else "drawing",
            "drawings": {},
            "winner": None,
            "blank_drawing": None,
            "room_code": room["code"],
        }
        room["status"] = "playing"
        for player_id in [*room["artists"], room["judge"]]:
            self.players[player_id]["status"] = "matched"
            self.players[player_id]["match_id"] = match_id

    def _new_room_code(self) -> str:
        alphabet = string.ascii_uppercase.replace("I", "").replace("O", "") + "23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(6))
            if code not in self.rooms:
                return code

    @staticmethod
    def _validate_role_mode(role: str, mode: str, artist_count: int) -> None:
        if role not in {"artist", "judge"}:
            raise ValueError("Choose artist or judge.")
        if mode not in {"prompted", "promptless", "speedpaint"}:
            raise ValueError("Choose prompted, promptless, or speedpaint.")
        if mode == "speedpaint" and artist_count != 2:
            raise ValueError("Speedpaint is a two-artist competition.")
        if artist_count not in {2, 4}:
            raise ValueError("Choose two or four artists.")

    @staticmethod
    def _clean_name(name: str) -> str:
        name = " ".join(name.strip().split())[:20]
        if not name:
            raise ValueError("Enter a username.")
        return name

    @staticmethod
    def _youtube_video_id(value: str) -> str:
        value = value.strip()
        parsed = urlparse(value if "://" in value else f"https://youtube.com/watch?v={value}")
        host = parsed.netloc.lower().removeprefix("www.")
        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
        elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/embed/", "/shorts/")):
                video_id = parsed.path.split("/")[2]
            else:
                video_id = ""
        else:
            video_id = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Enter a valid YouTube video URL or 11-character video ID.")
        return video_id

    def _queue_for(self, role: str, mode: str, artist_count: int) -> deque[str]:
        return self.queues[(mode, artist_count, role)]

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
        if parsed.path == "/api/stats":
            self._handle(STORE.stats)
            return
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
            "/api/room/create": self._create_room,
            "/api/room/join": self._join_room,
            "/api/submit": self._submit,
            "/api/vote": self._vote,
            "/api/requeue": self._requeue,
            "/api/leave": self._leave,
            "/api/blank": self._blank,
            "/api/music/select": self._select_music,
        }
        action = routes.get(urlparse(self.path).path)
        if action is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._handle(action)

    def _join(self) -> dict:
        body = self._body()
        return STORE.join(
            body.get("name", ""),
            body.get("role", ""),
            body.get("mode", ""),
            body.get("artist_count"),
        )

    def _create_room(self) -> dict:
        body = self._body()
        return STORE.create_room(
            body.get("name", ""),
            body.get("role", ""),
            body.get("mode", ""),
            body.get("artist_count"),
        )

    def _join_room(self) -> dict:
        body = self._body()
        return STORE.join_room(
            body.get("name", ""), body.get("room_code", ""), body.get("role", "")
        )

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

    def _select_music(self) -> dict:
        body = self._body()
        STORE.select_music(body.get("player_id", ""), body.get("song", ""))
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
