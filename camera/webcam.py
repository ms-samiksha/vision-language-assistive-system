"""Threaded webcam capture helper — Windows/CPU optimized."""
from __future__ import annotations

import threading
import time
from typing import Optional

import cv2


class WebcamStream:
    """
    Continuously grabs frames on a background thread for low-latency reads.

    Windows-specific:
    - Uses cv2.CAP_DSHOW backend (DirectShow) for faster open times on Windows.
    - Falls back to default backend if CAP_DSHOW fails (e.g., external USB cams
      that don't support DirectShow).
    - CAP_PROP_BUFFERSIZE=1 keeps buffer minimal so read() always returns
      the most recent frame.
    - Frame capture capped at ~30 FPS via time.sleep(0.033) to prevent
      saturating a CPU core.
    """

    def __init__(
        self,
        index:  int = 0,
        width:  int = 640,
        height: int = 480,
    ) -> None:
        self.index  = index
        self.width  = width
        self.height = height

        # Try DirectShow first (faster on Windows), fall back to default
        self.capture = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self.capture.isOpened():
            print(f"[WebcamStream] CAP_DSHOW failed for index {index}, trying default backend...")
            self.capture = cv2.VideoCapture(self.index)
        if not self.capture.isOpened():
            raise RuntimeError(
                f"Cannot open webcam index {index}. "
                "Check that your camera is connected and not used by another app."
            )

        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH,  float(self.width))
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1.0)

        # Optional: set MJPEG codec for faster USB cam transfers
        self.capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        self._frame_lock = threading.Lock()
        self._frame: Optional[cv2.typing.MatLike] = None
        self._running = False
        self._thread  = threading.Thread(
            target=self._update_loop, daemon=True, name="WebcamStream"
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> "WebcamStream":
        if self._running:
            return self
        self._running = True
        self._thread.start()
        # Warm up: wait up to 2 s for first frame
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with self._frame_lock:
                if self._frame is not None:
                    break
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self.capture.release()
        except Exception:
            pass

    # ── Background capture ────────────────────────────────────────────────────

    def _update_loop(self) -> None:
        consecutive_failures = 0
        while self._running:
            ret, frame = self.capture.read()
            if not ret or frame is None:
                consecutive_failures += 1
                if consecutive_failures > 30:
                    print("[WebcamStream] Too many read failures — stopping.", flush=True)
                    self._running = False
                    break
                time.sleep(0.05)
                continue

            consecutive_failures = 0
            with self._frame_lock:
                self._frame = frame

            time.sleep(0.033)   # ~30 FPS cap

    # ── Public API ────────────────────────────────────────────────────────────

    def read(self) -> Optional[cv2.typing.MatLike]:
        """Return a copy of the latest frame, or None if none captured yet."""
        with self._frame_lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def is_ready(self) -> bool:
        """True once at least one frame has been captured."""
        with self._frame_lock:
            return self._frame is not None

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "WebcamStream":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()