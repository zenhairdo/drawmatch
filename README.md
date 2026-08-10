# Drawmatch Beta

A browser-based multiplayer drawing match for two or four artists and one judge.
Players choose a temporary username before joining matchmaking or a private room.

## Run locally

```bash
python3 server.py
```

Open `http://localhost:8000` in three separate browsers or private windows.

## Speedpaint music

Speedpaint matches always use two artists and one judge. Each visible stroke
fades after three seconds, while the complete drawing is preserved for judging.

Set `YOUTUBE_TRACKS` to one or more comma-separated, embeddable YouTube video
IDs before starting the server:

```bash
YOUTUBE_TRACKS="video_id_one,video_id_two" python3 server.py
```

After matchmaking, the judge chooses one of these playlist tracks or pastes any
valid YouTube URL/video ID. That choice starts the round. Artist clients seek to
the shared round-start offset and resynchronize every five seconds. Playback
starts muted to satisfy browser autoplay rules; each artist can turn sound on.

During the round, each artist publishes a compressed preview about four times
per second. The judge sees both anonymous canvases in contestant order, polls
for updates every 300 milliseconds, and hears the same timestamp-synchronized
track. The final archived drawings remain separate from these fading live views.

## Deploy on Render

This repository includes `render.yaml` for a free Render web service. Push the
`drawmatch` directory to a GitHub repository, then create a new Render Blueprint
from that repository. Render supplies the public HTTPS URL automatically.

The beta stores queues and matches in memory. A server restart clears active
matches, and it must remain a single-instance service until that state moves to
a shared database.
