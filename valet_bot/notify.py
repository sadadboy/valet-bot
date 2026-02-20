from __future__ import annotations

import requests


def send_discord_success(webhook_url: str, content: str) -> None:
    if not webhook_url:
        return
    requests.post(webhook_url, json={"content": content}, timeout=10)
