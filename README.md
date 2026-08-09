# Drawmatch Beta

A browser-based multiplayer drawing match for two artists and one judge.
Players authenticate with Google before choosing a matchmaking role.

## Run locally

```bash
python3 server.py
```

Open `http://localhost:8000` in three separate browsers or private windows.

## Deploy on Render

This repository includes `render.yaml` for a free Render web service. Push the
`drawmatch` directory to a GitHub repository, then create a new Render Blueprint
from that repository. Render supplies the public HTTPS URL automatically.

The beta stores queues and matches in memory. A server restart clears active
matches, and it must remain a single-instance service until that state moves to
a shared database.
