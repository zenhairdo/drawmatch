# Drawmatch Beta

A browser-based multiplayer drawing match for two or four artists and one judge.
Players choose a temporary username before joining matchmaking or a private room.

## Run locally

```bash
python3 server.py
```

Open `http://localhost:8000` in three separate browsers or private windows.

## Speedpaint music

Speedpaint matches always use two artists and one judge. Each visible line
segment remains fully visible for three seconds, then the oldest segments are
removed first while the complete drawing is preserved for judging.

Set `YOUTUBE_TRACKS` to one or more comma-separated, embeddable YouTube video
IDs before starting the server:

```bash
YOUTUBE_TRACKS="video_id_one,video_id_two" python3 server.py
```

After matchmaking, the judge chooses one of these playlist tracks or pastes any
valid YouTube URL/video ID. That choice starts the round. Artist clients seek to
the shared round-start offset and resynchronize every second. The client
attempts audible autoplay first. If a browser blocks it, a **Play music** button
appears so playback can be unlocked with one user gesture.

During the round, each artist publishes a compressed preview about four times
per second. The judge sees both anonymous canvases in contestant order, polls
for updates every 300 milliseconds, and hears the same timestamp-synchronized
track. The final archived drawings remain separate from these expiring live views.

The home screen has two one-browser previews:

- **Watch bot speedpaint** removes the oldest segments after three seconds and
  supports optional YouTube music.
- **Watch bot drawing match** shows two bots building persistent paintings.

Both demos generate random live paint for 30 seconds and then continue to the
normal results view.

## Live stage

Choose **Enter live stage** to watch, perform, challenge, or vote. The first
artist takes an empty stage with a YouTube song that loops while they draw.
Stage canvases use the same three-second oldest-first disappearing ink and send
downscaled compressed live frames to the audience up to ten times per second.
Polling and uploads never overlap, preventing slow requests from creating a
backlog of stale frames.

A viewer can challenge the incumbent with a new YouTube song when the stage is
unlocked. The incumbent's already-playing song continues uninterrupted for 30
seconds, followed by the challenger's song. Non-contestant viewers then have 15
seconds to vote while both contestants keep drawing and the challenger song
continues. A tie keeps the incumbent on stage. The winner keeps the challenger
song playing from the same position, loops it, and receives 60 seconds of
protection before another challenge can begin.

Like matchmaking, stage sessions and votes are held in memory and reset when
the server restarts. Closing a stage tab sends an immediate leave signal; a
15-second heartbeat timeout removes disconnected performers, challengers, and
viewers if that signal cannot be delivered.

## Deploy on Render

This repository includes `render.yaml` for a free Render web service. Push the
`drawmatch` directory to a GitHub repository, then create a new Render Blueprint
from that repository. Render supplies the public HTTPS URL automatically.

The beta stores queues and matches in memory. A server restart clears active
matches, and it must remain a single-instance service until that state moves to
a shared database.
