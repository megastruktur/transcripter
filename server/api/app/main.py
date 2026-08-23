"""Transcripter API entrypoint."""

import hmac
import os
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import ServerConfig, load_config
from app.db import Base, engine, init_engine
from app.routes import recordings, regenerate, settings

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

cfg: ServerConfig = load_config()

app = FastAPI(title="Transcripter API")
# LAN clients: Tauri webview (tauri://localhost, http://localhost:5173 dev)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",  # single-user LAN; token is the auth boundary
    allow_methods=["*"],
    allow_headers=["*"],
)
app.state.config = cfg
app.state.on_finalize = lambda rec_id, duration: regenerate.trigger_pipeline_async(rec_id, duration)

init_engine(cfg.database.url)
Base.metadata.create_all(bind=engine())

app.include_router(recordings.router)
app.include_router(regenerate.router)
app.include_router(settings.router)


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    token = os.environ.get("TRANSCRIPTER_TOKEN", "")
    # This middleware is added after CORSMiddleware, so it wraps it: preflight
    # would be rejected here before CORS could answer. Preflight carries no
    # credentials by spec, so let OPTIONS through — the real request is authed.
    if token and request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {token}"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
