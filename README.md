# LEO - Personal AI Assistant

LEO is an ambient desktop AI assistant for Windows that I built from scratch in Python. It runs in the background, greets me with the day's agenda, watches for deadlines and new grades, and can see my screen, take voice input, control the desktop, remember across sessions, and even edit its own code.

**~1,500 lines of Python | hybrid cloud/local brain | runs daily**

## What it does

- **Conversational core with a brain switch** - routes between the Anthropic API (cloud) and a local Ollama model (offline).
- **Screen vision** - captures and interprets what's on screen.
- **Voice input** - listens and responds to spoken commands.
- **Desktop control** - moves the mouse, clicks, types, and drives UI elements, with a confirmation step before sensitive actions.
- **Persistent memory** - saves and recalls facts across sessions.
- **Self-editing** - can search, read, and safely edit its own source, with automatic backups and revert.
- **Deadline awareness** - pulls from Brightspace and Gradescope and surfaces what's due.

## Architecture

- `leo.py` - the engine: brain routing, tools, memory, vision, self-editing, and the Brightspace/Gradescope integrations.
- `leo_app.py` - a customtkinter GUI layer that runs on top of leo.py's brain.

## Setup

    python -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt

Copy .env.example to .env and add your keys (ANTHROPIC_API_KEY, GRADESCOPE_EMAIL, GRADESCOPE_PASSWORD), then run:

    python leo_app.py

## Configuration

All credentials are read from a local .env file, which is git-ignored and never committed.

---

Personal project - 2026 - Tri Beckham Nguyen
