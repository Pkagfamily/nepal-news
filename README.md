# Nepal News (engine)

Reads Nepal's news sites every ~10 minutes, has an AI write each story as a proper English news
report, and publishes it as a locked (encrypted) and signed file for the Nepal News app.

- `scripts/engine.py` – reads the sites, removes duplicates, publishes `feed`, `pulse`, `status.json`
- `scripts/ai.py` – Gemini (main) and GitHub AI models (backup); every story is checked
- branch `feed` – the published news (replaced on every run, no history kept)
- branch `app-download` – the latest app file and its signed version info
