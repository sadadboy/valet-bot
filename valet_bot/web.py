from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .automation import run_booking_list_cancel, run_booking_list_check
from .config import DEBUG_DIR, ROOT_DIR, SCREENSHOT_DIR, ConfigStore
from .scheduler import BookingScheduler
from .state import StateStore

app = FastAPI(title="Valet Booking Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.mount("/shots", StaticFiles(directory=str(ROOT_DIR / "screenshots")), name="shots")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/debug", StaticFiles(directory=str(DEBUG_DIR)), name="debug")

cfg = ConfigStore()
state = StateStore()
scheduler = BookingScheduler(cfg, state)


def _render_queue_text(config: dict[str, Any]) -> str:
    rows: list[str] = []
    for p in config.get("queue", {}).get("profiles", []) or []:
        rows.append(
            ",".join(
                [
                    str(p.get("name", "")).strip(),
                    str(p.get("phone", "")).strip(),
                    str(p.get("car_number", "")).strip(),
                    str(p.get("car_model", "")).strip(),
                ]
            )
        )
    return "\n".join(rows)


def _parse_queue_text(raw: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in raw.splitlines():
        row = line.strip()
        if not row or row.startswith("#"):
            continue
        parts = [x.strip() for x in row.split(",")]
        if len(parts) < 4:
            continue
        out.append(
            {
                "name": parts[0],
                "phone": parts[1],
                "car_number": parts[2],
                "car_model": parts[3],
            }
        )
    return out


def _profile_key(p: dict[str, Any]) -> str:
    return "|".join([str(p.get("name", "")).strip(), str(p.get("phone", "")).strip(), str(p.get("car_number", "")).strip()])


@app.on_event("startup")
def on_startup() -> None:
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.stop()


@app.get("/")
def index(request: Request) -> Any:
    # Self-heal scheduler in case worker thread died.
    scheduler.start()
    config = cfg.load()
    runtime_state = state.read_state()
    history = state.read_history(limit=30)
    queue = config.get("queue", {})
    profiles = queue.get("profiles", []) or []
    meta = queue.get("profile_meta", []) or []
    active_idx = int(queue.get("active_index", 0))
    if active_idx < 0:
        active_idx = 0
    if profiles and active_idx >= len(profiles):
        active_idx = len(profiles) - 1
    active_profile = profiles[active_idx] if profiles else None
    queue_rows = []
    for i, p in enumerate(profiles):
        m = meta[i] if i < len(meta) else {}
        queue_rows.append({"idx": i, "profile": p, "meta": m})
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "config": config,
            "runtime_state": runtime_state,
            "history": history,
            "queue_text": _render_queue_text(config),
            "queue_active_profile": active_profile,
            "queue_active_index": active_idx,
            "queue_total": len(profiles),
            "queue_rows": queue_rows,
        },
    )


@app.post("/config")
def save_config(
    name: str = Form(...),
    phone: str = Form(...),
    car_number: str = Form(...),
    car_model: str = Form(...),
    target_departure_date: str = Form(...),
    departure_time: str = Form(...),
    target_arrival_date: str = Form(...),
    arrival_time: str = Form(...),
    check_start_time: str = Form(...),
    stop_time: str = Form(...),
    interval_seconds: int = Form(...),
    service_type: str = Form(...),
    brand: str = Form(...),
    color: str = Form(...),
    discount_type: str = Form(...),
    airline: str = Form(...),
    discord_webhook_url: str = Form(...),
    enabled: str = Form("false"),
    headless: str = Form("true"),
    test_skip_dates: str = Form("false"),
    queue_enabled: str = Form("false"),
    queue_profiles_text: str = Form(""),
) -> RedirectResponse:
    data = cfg.load()
    data["booking"].update(
        {
            "name": name,
            "phone": phone,
            "car_number": car_number,
            "car_model": car_model,
            "service_type": service_type,
            "brand": brand,
            "color": color,
            "discount_type": discount_type,
            "airline": airline,
        }
    )
    data["schedule"].update(
        {
            "target_departure_date": target_departure_date,
            "departure_time": departure_time,
            "target_arrival_date": target_arrival_date,
            "arrival_time": arrival_time,
            "check_start_time": check_start_time,
            "stop_time": stop_time,
            "interval_seconds": max(5, interval_seconds),
        }
    )
    data["notify"]["discord_webhook_url"] = discord_webhook_url
    data["general"]["enabled"] = enabled == "true"
    data["runtime"]["headless"] = headless == "true"
    data["runtime"]["test_skip_dates"] = test_skip_dates == "true"
    queue_profiles = _parse_queue_text(queue_profiles_text)
    queue = data.get("queue", {})
    queue["enabled"] = queue_enabled == "true"
    old_profiles = queue.get("profiles", []) or []
    old_meta = queue.get("profile_meta", []) or []
    old_map = {}
    for i, p in enumerate(old_profiles):
        k = _profile_key(p)
        if not k:
            continue
        old_map[k] = old_meta[i] if i < len(old_meta) else {}
    queue["profiles"] = queue_profiles
    new_meta = []
    for p in queue_profiles:
        k = _profile_key(p)
        new_meta.append(old_map.get(k, {"status": "대기", "last_message": "", "last_at": "", "success_count": 0, "fail_count": 0}))
    queue["profile_meta"] = new_meta
    if queue_profiles:
        current_idx = int(queue.get("active_index", 0))
        if current_idx < 0:
            current_idx = 0
        if current_idx >= len(queue_profiles):
            current_idx = len(queue_profiles) - 1
        queue["active_index"] = current_idx
    else:
        queue["active_index"] = 0
    data["queue"] = queue
    cfg.save(data)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queue/next")
def queue_next() -> RedirectResponse:
    data = cfg.load()
    q = data.get("queue", {})
    profiles = q.get("profiles", []) or []
    if profiles:
        idx = int(q.get("active_index", 0))
        if idx < len(profiles) - 1:
            q["active_index"] = idx + 1
            data["queue"] = q
            cfg.save(data)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queue/prev")
def queue_prev() -> RedirectResponse:
    data = cfg.load()
    q = data.get("queue", {})
    profiles = q.get("profiles", []) or []
    if profiles:
        idx = int(q.get("active_index", 0))
        if idx > 0:
            q["active_index"] = idx - 1
            data["queue"] = q
            cfg.save(data)
    return RedirectResponse(url="/", status_code=303)


@app.post("/queue/reset")
def queue_reset() -> RedirectResponse:
    data = cfg.load()
    q = data.get("queue", {})
    q["active_index"] = 0
    data["queue"] = q
    cfg.save(data)
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-now")
def run_now() -> RedirectResponse:
    scheduler.trigger_now()
    return RedirectResponse(url="/", status_code=303)


@app.post("/history/verify")
def verify_history(ts: str = Form(...)) -> RedirectResponse:
    rows = state.read_history(limit=500)
    target = next((r for r in rows if r.get("ts") == ts), None)
    if not target:
        return RedirectResponse(url="/", status_code=303)
    cfg_data = cfg.load()
    booking = target.get("booking", {}) or {}
    verify_result = run_booking_list_check(cfg_data, SCREENSHOT_DIR, booking_override=booking)

    def updater(row: dict[str, Any]) -> dict[str, Any]:
        if row.get("ts") != ts:
            return row
        result = dict(row.get("result", {}))
        if result.get("status") == "canceled_by_user":
            # Keep canceled rows stable; do not overwrite with re-verify state.
            result["booking_list"] = verify_result
            row["result"] = result
            return row
        result["booking_list"] = verify_result
        if verify_result.get("ok"):
            result["ok"] = True
            result["status"] = "success_verified"
            result["message"] = f"booking_list_verified:{verify_result.get('message','')}"
        else:
            result["ok"] = False
            result["status"] = "success_unverified"
            result["message"] = f"booking_list_unverified:{verify_result.get('message','')}"
        row["result"] = result
        return row

    state.update_history_by_ts(ts, updater)
    return RedirectResponse(url="/", status_code=303)


@app.post("/history/cancel")
def cancel_history(ts: str = Form(...)) -> RedirectResponse:
    rows = state.read_history(limit=500)
    target = next((r for r in rows if r.get("ts") == ts), None)
    if not target:
        return RedirectResponse(url="/", status_code=303)
    cfg_data = cfg.load()
    booking = target.get("booking", {}) or {}
    cancel_result = run_booking_list_cancel(cfg_data, SCREENSHOT_DIR, booking_override=booking)

    def updater(row: dict[str, Any]) -> dict[str, Any]:
        if row.get("ts") != ts:
            return row
        result = dict(row.get("result", {}))
        result["cancel_result"] = cancel_result
        if cancel_result.get("ok"):
            result["status"] = "canceled_by_user"
            result["ok"] = False
            result["message"] = f"예약취소 완료:{cancel_result.get('message','')}"
            result["booking_list"] = {
                "ok": True,
                "message": "booking_list_status:취소;cancel_available=False",
                "status_text": "취소",
                "cancel_available": False,
                "screenshot_path": cancel_result.get("screenshot_path", ""),
            }
            if cancel_result.get("screenshot_path"):
                result["screenshot_path"] = cancel_result.get("screenshot_path")
        else:
            result["message"] = f"예약취소 실패:{cancel_result.get('message','')}"
        row["result"] = result
        return row

    state.update_history_by_ts(ts, updater)
    return RedirectResponse(url="/", status_code=303)
