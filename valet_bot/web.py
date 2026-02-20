from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import DEBUG_DIR, ROOT_DIR, ConfigStore
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
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"config": config, "runtime_state": runtime_state, "history": history},
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
    cfg.save(data)
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-now")
def run_now() -> RedirectResponse:
    scheduler.trigger_now()
    return RedirectResponse(url="/", status_code=303)
