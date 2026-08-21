"""Transcripter API entrypoint."""

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Transcripter API")

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json"}


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    token = os.environ.get("TRANSCRIPTER_TOKEN", "")
    if token and request.url.path not in PUBLIC_PATHS:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
