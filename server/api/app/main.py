"""Transcripter API entrypoint."""

import hmac
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Transcripter API")

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}


def _check_startup() -> None:
    config_path = os.environ.get("TRANSCRIPTER_CONFIG", "/etc/transcripter/config.yaml")
    if os.path.isdir(config_path):
        sys.exit(
            f"config path {config_path} is a directory — this usually means the "
            "compose bind-mount found no config.yaml on the host. "
            "Copy server/config.example.yaml to server/config.yaml first."
        )
    if not os.path.exists(config_path):
        sys.exit(
            f"config file {config_path} not found — "
            "copy server/config.example.yaml to server/config.yaml and restart."
        )


_check_startup()


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    token = os.environ.get("TRANSCRIPTER_TOKEN", "")
    if token and request.url.path not in PUBLIC_PATHS:
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {token}"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
