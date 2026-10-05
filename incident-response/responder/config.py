"""Settings, all overridable through environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # incident-response/

READ_ONLY_TOOLS = "Read,Grep,Glob"
FIX_TOOLS = ",".join(
    [
        "Read", "Grep", "Glob", "Edit", "Write",
        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
        "Bash(uv run:*)", "Bash(curl:*)",
        "Bash(docker compose ps:*)", "Bash(docker compose logs:*)",
        "Bash(docker compose up:*)", "Bash(docker compose restart:*)",
        "Bash(cat:*)", "Bash(ls:*)",
    ]
)
# Hard stops, even if the model is asked (or tricked) into trying.
DENIED_TOOLS = ",".join(
    [
        "Bash(git commit:*)", "Bash(git push:*)", "Bash(git reset:*)", "Bash(git checkout:*)",
        "Bash(docker compose down:*)", "Bash(docker system:*)", "Bash(docker volume:*)",
        "Bash(rm:*)", "Bash(sudo:*)", "WebFetch", "WebSearch",
    ]
)


@dataclass
class Settings:
    repo_root: Path = field(default_factory=lambda: Path(os.getenv("REPO_ROOT", HERE.parent)))
    incidents_dir: Path = field(default_factory=lambda: Path(os.getenv("INCIDENTS_DIR", HERE / "incidents")))
    agent_command: str = field(default_factory=lambda: os.getenv("AGENT_COMMAND", "claude"))
    agent_timeout: int = field(default_factory=lambda: int(os.getenv("AGENT_TIMEOUT", "900")))
    agent_max_turns: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_TURNS", "40")))
    cooldown_seconds: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_SECONDS", "600")))
    loki_url: str = field(default_factory=lambda: os.getenv("LOKI_URL", "http://localhost:3100"))
    tempo_url: str = field(default_factory=lambda: os.getenv("TEMPO_URL", "http://localhost:3200"))
    prometheus_url: str = field(default_factory=lambda: os.getenv("PROMETHEUS_URL", "http://localhost:9090"))
    app_url: str = field(default_factory=lambda: os.getenv("APP_URL", "http://localhost:8000"))
    lookback_minutes: int = field(default_factory=lambda: int(os.getenv("LOOKBACK_MINUTES", "15")))
