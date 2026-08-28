"""Transcriptor API entrypoint."""

import hmac
import os
import re
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import ServerConfig, load_config
from app.db import STAGE_KINDS, Base, engine, init_engine
from app.routes import profiles as profiles_route
from app.routes import recordings, regenerate, settings, tags

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}
# <audio> elements cannot send Authorization, so the bearer middleware below
# additionally accepts ?token= — on this exact path shape only.
AUDIO_PATH_RE = re.compile(r"^/recordings/[0-9a-f-]{36}/audio$")


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

app = FastAPI(title="Transcriptor API")
# LAN clients: Tauri webview (tauri://localhost, http://localhost:5173 dev)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",  # single-user LAN; token is the auth boundary
    allow_methods="*",
    allow_headers="*",
)
app.state.config = cfg
app.state.on_finalize = lambda rec_id, duration: regenerate.trigger_pipeline_async(rec_id, duration)
init_engine(cfg.database.url)
Base.metadata.create_all(bind=engine())


def _migrate_stage_kind_enum() -> None:
    """create_all never alters an EXISTING Postgres enum: databases created
    before a new stage kind (e.g. `chunk`) need their stage_kind type
    extended explicitly. Idempotent; SQLite test databases skip it (their
    Enum is a CHECK constraint rebuilt with the schema)."""
    if engine().dialect.name != "postgresql":
        return
    from sqlalchemy import text

    with engine().begin() as conn:
        for kind in STAGE_KINDS:
            conn.execute(text(f"ALTER TYPE stage_kind ADD VALUE IF NOT EXISTS '{kind}'"))


def _migrate_tags_column() -> None:
    """Backfill `tags` for databases created before the knowledge-graph
    feature landed. create_all only sees the CURRENT schema, so pre-tags
    Postgres recordings tables get the column added at startup. Tagged
    recordings get a GIN index for substring-array queries (the ?q=
    path falls back to a cast + ilike today, but the index is cheap
    insurance and matches the contract documented in the wave-A plan).
    Idempotent: ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
    SQLite has no TEXT[]; create_all already builds the JSON variant."""
    if engine().dialect.name != "postgresql":
        return
    from sqlalchemy import text

    with engine().begin() as conn:
        conn.execute(
            text("ALTER TABLE recordings ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_recordings_tags ON recordings USING GIN (tags)")
        )


_migrate_stage_kind_enum()
_migrate_tags_column()

app.include_router(recordings.router)
app.include_router(regenerate.router)
app.include_router(settings.router)
app.include_router(profiles_route.router)
app.include_router(tags.router)

@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    token = os.environ.get("TRANSCRIPTER_TOKEN", "")
    # This middleware is added after CORSMiddleware, so it wraps it: preflight
    # would be rejected here before CORS could answer. Preflight carries no
    # credentials by spec, so let OPTIONS through — the real request is authed.
    if token and request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        auth = request.headers.get("authorization", "")
        ok = hmac.compare_digest(auth, f"Bearer {token}")
        # Query-token fallback for <audio src>: exact audio path only, still
        # constant-time compared. Every other route stays header-only — a
        # ?token= there (no valid header) is a 401 like before.
        if not ok and AUDIO_PATH_RE.match(request.url.path):
            ok = hmac.compare_digest(request.query_params.get("token", ""), token)
        if not ok:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
