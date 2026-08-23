"""Read-only settings view (values, never secrets)."""

from fastapi import APIRouter, Request

from app.config import SummarizeConfig, TranscribeConfig

router = APIRouter(prefix="/settings")


@router.get("")
def get_settings(request: Request) -> dict:
    cfg = request.app.state.config

    def mask(v: str) -> str:
        return "***" if v else ""

    return {
        "transcribe": {
            "backend": cfg.transcribe.backend,
            "model": cfg.transcribe.model,
            "base_url": mask(cfg.transcribe.base_url),
        },
        "summarize": {
            "enabled": cfg.summarize.enabled,
            "model": cfg.summarize.model,
            "base_url": mask(cfg.summarize.base_url),
        },
        "diarization": {
            "endpoint": cfg.diarization.endpoint,
            "enabled": cfg.diarization.enabled,
        },
    }


def summarize_active(s: SummarizeConfig) -> bool:
    return s.enabled and bool(s.model)


def transcribe_backend(t: TranscribeConfig) -> str:
    return t.backend
