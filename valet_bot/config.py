from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.yaml"
STATE_PATH = ROOT_DIR / "data" / "state.json"
HISTORY_PATH = ROOT_DIR / "data" / "history.jsonl"
SCREENSHOT_DIR = ROOT_DIR / "screenshots"
DEBUG_DIR = ROOT_DIR / "data" / "debug"

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "timezone": "Asia/Seoul",
        "enabled": True,
    },
    "schedule": {
        "target_departure_date": "2026-04-22",
        "departure_time": "00:00",
        "target_arrival_date": "2026-04-24",
        "arrival_time": "00:00",
        "check_start_time": "00:00",
        "stop_time": "02:00",
        "interval_seconds": 30,
    },
    "booking": {
        "name": "박홍원",
        "phone": "01076311377",
        "car_number": "64버3059",
        "car_model": "GV60",
        "service_type": "일반",
        "brand": "제네시스",
        "color": "검정",
        "discount_type": "일반",
        "airline": "대한항공",
    },
    "queue": {
        "enabled": False,
        "active_index": 0,
        "profiles": [],
        "profile_meta": [],
    },
    "notify": {
        "discord_webhook_url": "https://discord.com/api/webhooks/1392528407685894146/rwSBXQfFEeck2XWHbx91QHgMMyiQrW46p4QojCIUxSonhYHIEbjLN-FRD0cjEa5cAVzR",
        "success_only": True,
    },
    "runtime": {
        "headless": True,
        "slow_mo_ms": 0,
        "timeout_ms": 15000,
        "debug_enabled": True,
        "test_skip_dates": False,
    },
}


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_CONFIG)

    def load(self) -> dict[str, Any]:
        with self._lock:
            with self.path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        return self._merge_defaults(data)

    def save(self, data: dict[str, Any]) -> None:
        merged = self._merge_defaults(data)
        with self._lock:
            with self.path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)

    def _merge_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
            out = dict(base)
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(out.get(key), dict):
                    out[key] = merge(out[key], value)
                else:
                    out[key] = value
            return out

        return merge(DEFAULT_CONFIG, data or {})
