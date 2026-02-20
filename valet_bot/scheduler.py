from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from .automation import run_booking_attempt
from .config import SCREENSHOT_DIR, ConfigStore
from .notify import send_discord_success
from .state import StateStore


class BookingScheduler:
    def __init__(self, cfg: ConfigStore, state: StateStore) -> None:
        self.cfg = cfg
        self.state = state
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def trigger_now(self) -> None:
        if self._run_lock.locked():
            return
        threading.Thread(target=self._attempt_once, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                # Keep scheduler alive even if one tick fails.
                state = self.state.read_state()
                state["scheduler_error"] = str(exc)
                self.state.write_state(state)
            time.sleep(3)

    def _tick(self) -> None:
        config = self.cfg.load()
        if not config["general"]["enabled"]:
            return

        tz = ZoneInfo(config["general"]["timezone"])
        now = datetime.now(tz)
        schedule = config["schedule"]
        target = schedule["target_departure_date"]

        start_at = self._parse_time(schedule["check_start_time"])
        stop_at = self._parse_time(schedule["stop_time"])
        if not self._in_window(now.time(), start_at, stop_at):
            return

        state = self.state.read_state()
        if state.get("last_success_target") == target:
            return

        last_attempt = state.get("last_attempt_at")
        if last_attempt:
            try:
                prev = datetime.fromisoformat(last_attempt)
                delta = (datetime.now() - prev).total_seconds()
                if delta < int(schedule["interval_seconds"]):
                    return
            except Exception:
                pass

        self._attempt_once()

    def _attempt_once(self) -> None:
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            config = self.cfg.load()
            state = self.state.read_state()
            state["running"] = True
            self.state.write_state(state)

            result = run_booking_attempt(config, SCREENSHOT_DIR)

            updated = self.state.read_state()
            updated["running"] = False
            updated["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
            if result["ok"]:
                updated["last_success_target"] = config["schedule"]["target_departure_date"]
            self.state.write_state(updated)

            record = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "target_departure_date": config["schedule"]["target_departure_date"],
                "departure_time": config["schedule"]["departure_time"],
                "arrival_date": config["schedule"]["target_arrival_date"],
                "arrival_time": config["schedule"]["arrival_time"],
                "result": result,
            }
            self.state.append_history(record)

            if result["ok"] and config["notify"]["discord_webhook_url"]:
                msg = (
                    f"[성공] 예약 완료 가능성 감지\n"
                    f"- 대상일: {config['schedule']['target_departure_date']} {config['schedule']['departure_time']}\n"
                    f"- 메시지: {result['message']}"
                )
                send_discord_success(config["notify"]["discord_webhook_url"], msg)
        finally:
            self._run_lock.release()

    @staticmethod
    def _parse_time(value: str) -> dtime:
        hh, mm = value.split(":")
        return dtime(hour=int(hh), minute=int(mm))

    @staticmethod
    def _in_window(now: dtime, start: dtime, stop: dtime) -> bool:
        if start <= stop:
            return start <= now <= stop
        return now >= start or now <= stop
