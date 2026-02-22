from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .config import HISTORY_PATH, SCREENSHOT_DIR, STATE_PATH


@dataclass
class AttemptResult:
    ok: bool
    status: str
    message: str
    screenshot_path: str | None = None


class StateStore:
    def __init__(self, state_path: Path = STATE_PATH, history_path: Path = HISTORY_PATH) -> None:
        self.state_path = state_path
        self.history_path = history_path
        self.lock = Lock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def read_state(self) -> dict[str, Any]:
        with self.lock:
            if not self.state_path.exists():
                return {
                    "last_attempt_at": None,
                    "last_success_target": None,
                    "last_success_key": None,
                    "running": False,
                }
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if "last_success_key" not in data:
                    data["last_success_key"] = None
                return data

    def write_state(self, state: dict[str, Any]) -> None:
        with self.lock:
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

    def append_history(self, record: dict[str, Any]) -> None:
        with self.lock:
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            if not self.history_path.exists():
                return []
            with self.history_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        rows = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(rows))

    def update_history_by_ts(self, ts: str, updater) -> bool:
        with self.lock:
            if not self.history_path.exists():
                return False
            with self.history_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            changed = False
            out_lines: list[str] = []
            for line in lines:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    out_lines.append(line)
                    continue
                if not changed and row.get("ts") == ts:
                    row = updater(row) or row
                    changed = True
                out_lines.append(json.dumps(row, ensure_ascii=False) + "\n")
            if changed:
                with self.history_path.open("w", encoding="utf-8") as f:
                    f.writelines(out_lines)
            return changed

    @staticmethod
    def now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")
