from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Event:
    team: str
    worker_id: str
    type: str
    payload: dict[str, Any]
    timestamp: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "timestamp": self.timestamp or datetime.now(UTC).isoformat(timespec="seconds"),
                "team": self.team,
                "worker_id": self.worker_id,
                "type": self.type,
                "payload": self.payload,
            }
        )


class EventBus:
    """Append-only file-based event log (JSON Lines).

    Workers invoked via the Task tool cannot share memory, so the bus is
    persisted as `events.jsonl` inside the decode output directory. Workers
    append events for other teams/phases to consume.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, event: Event) -> None:
        line = event.to_json()
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def by_team(self, team: str) -> list[dict[str, Any]]:
        return [e for e in self.events() if e.get("team") == team]
