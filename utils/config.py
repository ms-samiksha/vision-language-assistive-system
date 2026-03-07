"""Application configuration — Windows CPU optimized."""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class AppConfig:
    """
    Centralized config. frozen=True prevents accidental mutation.

    All timing fields are in milliseconds (*_ms).
    Use the @property helpers (_s suffix) anywhere you need seconds.
    """

    camera_index:  int = 0
    frame_width:   int = 640    # 640×480 opens fast and is plenty for BLIP
    frame_height:  int = 480

    # How often the background worker runs BLIP (ms).
    # 4000 ms = one inference every 4s — good for CPU, scene changes detected fast enough.
    sample_interval_ms: int = 4000

    window_name:   str   = "See & Tell"
    model_name:    str   = "Salesforce/blip-image-captioning-base"
    smoothing_window: int = 3       # majority-vote window

    confidence_threshold: float = 0.35

    # Max tokens BLIP generates. 40 = descriptive but still fast on CPU.
    max_caption_tokens: int = 40

    device: str = "cpu"             # always CPU on a Windows laptop

    # Downscale short-side to this before feeding BLIP.
    inference_short_side: int = 256

    # How long the background worker sleeps when no new frame is ready (ms).
    worker_idle_sleep_ms: int = 200

    # Minimum gap between captions sent to UI / TTS (ms).
    # SceneChangeDetector handles the real filtering — this is just a safety floor.
    caption_delay_ms: int = 4000

    # ── Convenience second-based properties ──────────────────────────────────
    @property
    def sample_interval_s(self) -> float:
        return self.sample_interval_ms / 1000.0

    @property
    def worker_idle_sleep_s(self) -> float:
        return self.worker_idle_sleep_ms / 1000.0

    @property
    def caption_delay_s(self) -> float:
        return self.caption_delay_ms / 1000.0


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def load_config() -> AppConfig:
    """Build config, allowing env-var overrides for any field."""

    def _int(key: str, default: int) -> int:
        try:
            return int(_env(key, str(default)))
        except ValueError:
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(_env(key, str(default)))
        except ValueError:
            return default

    return AppConfig(
        camera_index          = _int  ("APP_CAMERA_INDEX",         AppConfig.camera_index),
        frame_width           = _int  ("APP_FRAME_WIDTH",          AppConfig.frame_width),
        frame_height          = _int  ("APP_FRAME_HEIGHT",         AppConfig.frame_height),
        sample_interval_ms    = _int  ("APP_SAMPLE_INTERVAL",      AppConfig.sample_interval_ms),
        window_name           = _env  ("APP_WINDOW_NAME",          AppConfig.window_name),
        model_name            = _env  ("APP_MODEL_NAME",           AppConfig.model_name),
        smoothing_window      = _int  ("APP_SMOOTHING_WINDOW",     AppConfig.smoothing_window),
        confidence_threshold  = _float("APP_CONFIDENCE_THRESHOLD", AppConfig.confidence_threshold),
        max_caption_tokens    = _int  ("APP_MAX_CAPTION_TOKENS",   AppConfig.max_caption_tokens),
        device                = _env  ("APP_DEVICE",               AppConfig.device),
        inference_short_side  = _int  ("APP_INFERENCE_SHORT_SIDE", AppConfig.inference_short_side),
        worker_idle_sleep_ms  = _int  ("APP_WORKER_IDLE_SLEEP_MS", AppConfig.worker_idle_sleep_ms),
        caption_delay_ms      = _int  ("APP_CAPTION_DELAY_MS",     AppConfig.caption_delay_ms),
    )