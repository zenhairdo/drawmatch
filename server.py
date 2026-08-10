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
STAGE_BATTLE_SECONDS = 60
STAGE_SONG_PHASE_SECONDS = 30
STAGE_VOTE_SECONDS = 15
STAGE_WINNER_COOLDOWN_SECONDS = 60
STAGE_ONLINE_TIMEOUT_SECONDS = 15
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
            if (
                player["role"] == "judge"
                and match["mode"] == "speedpaint"
                and match["status"] == "drawing"
            ):
                response["live_drawings"] = [
                    match["live_drawings"].get(artist) for artist in match["artists"]
                ]
                response["live_updated_at"] = [
                    match["live_updated_at"].get(artist) for artist in match["artists"]
                ]
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

    def update_live_drawing(self, player_id: str, drawing: str) -> None:
        if not drawing.startswith(("data:image/webp;base64,", "data:image/jpeg;base64,")):
            raise ValueError("Live drawing must be a WebP or JPEG image.")
        with self.lock:
            player = self._player(player_id)
            if player["role"] != "artist" or not player["match_id"]:
                raise ValueError("Only a matched artist can publish a live drawing.")
            match = self.matches[player["match_id"]]
            if match["mode"] != "speedpaint" or match["status"] != "drawing":
                raise ValueError("This speedpaint round is not live.")
            match["live_drawings"][player_id] = drawing
            match["live_updated_at"][player_id] = time.time()

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
                        "live_drawings": {},
                        "live_updated_at": {},
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
            "live_drawings": {},
            "live_updated_at": {},
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


class StageStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.players: dict[str, dict] = {}
        self.stage = self._empty_stage()

    @staticmethod
    def _empty_stage() -> dict:
        return {
            "status": "empty",
            "performer": None,
            "challenger": None,
            "song_id": "",
            "old_song_id": "",
            "new_song_id": "",
            "song_started_at": None,
            "battle_started_at": None,
            "battle_deadline": None,
            "vote_deadline": None,
            "cooldown_until": 0,
            "live_drawings": {},
            "votes": {},
            "last_winner": None,
        }

    def join(self, name: str) -> dict:
        name = GameStore._clean_name(name)
        with self.lock:
            self._expire()
            if any(player["name"].casefold() == name.casefold() for player in self.players.values()):
                raise ValueError("That stage name is already in use.")
            player_id = secrets.token_urlsafe(18)
            self.players[player_id] = {"id": player_id, "name": name, "last_seen": time.time()}
            return {"stage_player_id": player_id}

    def state_for(self, player_id: str) -> dict:
        with self.lock:
            player = self._player(player_id)
            player["last_seen"] = time.time()
            self._expire(active_player_id=player_id)
            stage = self.stage
            response = {
                "status": stage["status"],
                "name": player["name"],
                "viewer_count": len(self.players),
                "server_time": time.time(),
                "fade_seconds": SPEEDPAINT_FADE_SECONDS,
                "is_performer": player_id == stage["performer"],
                "is_challenger": player_id == stage["challenger"],
            }
            if stage["status"] == "empty":
                return response

            response.update(
                performer_name=self.players[stage["performer"]]["name"],
                song_id=stage["song_id"],
                song_started_at=stage["song_started_at"],
                cooldown_until=stage["cooldown_until"],
                last_winner_name=(
                    self.players[stage["last_winner"]]["name"]
                    if stage["last_winner"] in self.players
                    else None
                ),
            )
            if stage["status"] == "live":
                response.update(
                    live_drawing=stage["live_drawings"].get(stage["performer"]),
                    can_challenge=(
                        player_id != stage["performer"]
                        and time.time() >= stage["cooldown_until"]
                    ),
                )
            else:
                contestants = [stage["performer"], stage["challenger"]]
                response.update(
                    contestant_names=[self.players[item]["name"] for item in contestants],
                    live_drawings=[stage["live_drawings"].get(item) for item in contestants],
                    old_song_id=stage["old_song_id"],
                    new_song_id=stage["new_song_id"],
                    battle_started_at=stage["battle_started_at"],
                    battle_deadline=stage["battle_deadline"],
                    song_phase_seconds=STAGE_SONG_PHASE_SECONDS,
                    contestant_index=(contestants.index(player_id) if player_id in contestants else None),
                )
                if stage["status"] == "voting":
                    response.update(
                        vote_deadline=stage["vote_deadline"],
                        can_vote=player_id not in contestants and player_id not in stage["votes"],
                        vote_counts=[
                            sum(choice == index for choice in stage["votes"].values())
                            for index in range(2)
                        ],
                    )
            return response

    def take_stage(self, player_id: str, song: str) -> None:
        with self.lock:
            self._player(player_id)
            if self.stage["status"] != "empty":
                raise ValueError("The stage already has a performer.")
            now = time.time()
            self.stage.update(
                status="live",
                performer=player_id,
                song_id=GameStore._youtube_video_id(song),
                song_started_at=now,
                cooldown_until=now,
                live_drawings={},
            )

    def challenge(self, player_id: str, song: str) -> None:
        with self.lock:
            self._player(player_id)
            stage = self.stage
            now = time.time()
            if stage["status"] != "live":
                raise ValueError("The stage is not accepting challenges.")
            if player_id == stage["performer"]:
                raise ValueError("The stage performer cannot challenge themselves.")
            if now < stage["cooldown_until"]:
                raise ValueError("The winner is still in their protected minute.")
            new_song = GameStore._youtube_video_id(song)
            stage.update(
                status="battle",
                challenger=player_id,
                old_song_id=stage["song_id"],
                new_song_id=new_song,
                battle_started_at=now,
                battle_deadline=now + STAGE_BATTLE_SECONDS,
                vote_deadline=None,
                votes={},
            )
            stage["live_drawings"].pop(player_id, None)

    def update_live_drawing(self, player_id: str, drawing: str) -> None:
        if not drawing.startswith(("data:image/webp;base64,", "data:image/jpeg;base64,")):
            raise ValueError("Stage drawing must be a WebP or JPEG image.")
        with self.lock:
            self._expire()
            self._player(player_id)
            stage = self.stage
            allowed = player_id == stage["performer"] or (
                stage["status"] == "battle" and player_id == stage["challenger"]
            )
            if stage["status"] not in {"live", "battle"} or not allowed:
                raise ValueError("Only a current stage artist can publish a drawing.")
            stage["live_drawings"][player_id] = drawing

    def vote(self, player_id: str, choice: int) -> None:
        with self.lock:
            self._expire()
            self._player(player_id)
            stage = self.stage
            contestants = {stage["performer"], stage["challenger"]}
            if stage["status"] != "voting" or choice not in {0, 1}:
                raise ValueError("Stage voting is not open.")
            if player_id in contestants:
                raise ValueError("Contestants cannot vote in their own battle.")
            if player_id in stage["votes"]:
                raise ValueError("You already voted in this battle.")
            stage["votes"][player_id] = choice

    def leave(self, player_id: str) -> None:
        with self.lock:
            self._player(player_id)
            self._remove_player(player_id)

    def _remove_player(self, player_id: str) -> None:
        stage = self.stage
        if player_id == stage["performer"]:
            self.stage = self._empty_stage()
        elif player_id == stage["challenger"]:
            now = time.time()
            stage.update(
                status="live",
                challenger=None,
                song_id=stage["old_song_id"],
                song_started_at=now,
                cooldown_until=now,
                votes={},
            )
            stage["live_drawings"].pop(player_id, None)
        else:
            stage["votes"].pop(player_id, None)
        self.players.pop(player_id, None)

    def _expire(self, active_player_id: str | None = None) -> None:
        stage = self.stage
        now = time.time()
        cutoff = now - STAGE_ONLINE_TIMEOUT_SECONDS
        stale_players = [
            player_id
            for player_id, player in self.players.items()
            if player_id != active_player_id and player["last_seen"] < cutoff
        ]
        for player_id in stale_players:
            self._remove_player(player_id)
        stage = self.stage
        if stage["status"] == "battle" and now >= stage["battle_deadline"]:
            stage["status"] = "voting"
            stage["vote_deadline"] = now + STAGE_VOTE_SECONDS
        if stage["status"] == "voting" and now >= stage["vote_deadline"]:
            incumbent_votes = sum(choice == 0 for choice in stage["votes"].values())
            challenger_votes = sum(choice == 1 for choice in stage["votes"].values())
            winner = stage["challenger"] if challenger_votes > incumbent_votes else stage["performer"]
            winning_drawing = stage["live_drawings"].get(winner)
            stage.update(
                status="live",
                performer=winner,
                challenger=None,
                song_id=stage["new_song_id"],
                song_started_at=now,
                cooldown_until=now + STAGE_WINNER_COOLDOWN_SECONDS,
                live_drawings={winner: winning_drawing} if winning_drawing else {},
                votes={},
                last_winner=winner,
            )

    def _player(self, player_id: str) -> dict:
        try:
            return self.players[player_id]
        except KeyError as error:
            raise ValueError("Stage session not found.") from error


STORE = GameStore()
STAGE = StageStore()


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
        if parsed.path == "/api/stage/state":
            player_id = parse_qs(parsed.query).get("stage_player_id", [""])[0]
            self._handle(lambda: STAGE.state_for(player_id))
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
            "/api/live": self._live,
            "/api/stage/join": self._stage_join,
            "/api/stage/take": self._stage_take,
            "/api/stage/challenge": self._stage_challenge,
            "/api/stage/live": self._stage_live,
            "/api/stage/vote": self._stage_vote,
            "/api/stage/leave": self._stage_leave,
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

    def _live(self) -> dict:
        body = self._body()
        STORE.update_live_drawing(body.get("player_id", ""), body.get("drawing", ""))
        return {"ok": True}

    def _stage_join(self) -> dict:
        return STAGE.join(self._body().get("name", ""))

    def _stage_take(self) -> dict:
        body = self._body()
        STAGE.take_stage(body.get("stage_player_id", ""), body.get("song", ""))
        return {"ok": True}

    def _stage_challenge(self) -> dict:
        body = self._body()
        STAGE.challenge(body.get("stage_player_id", ""), body.get("song", ""))
        return {"ok": True}

    def _stage_live(self) -> dict:
        body = self._body()
        STAGE.update_live_drawing(
            body.get("stage_player_id", ""), body.get("drawing", "")
        )
        return {"ok": True}

    def _stage_vote(self) -> dict:
        body = self._body()
        STAGE.vote(body.get("stage_player_id", ""), body.get("choice"))
        return {"ok": True}

    def _stage_leave(self) -> dict:
        STAGE.leave(self._body().get("stage_player_id", ""))
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
