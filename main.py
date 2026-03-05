"""Entry point for the SmolVLM real-time webcam demo."""
from __future__ import annotations

import sys
import textwrap
import time
import threading
from typing import List

import cv2

from camera.webcam import WebcamStream
from utils.config import load_config
from utils.fps import FPSCounter
from vision.inference import FrameAnalyzer
from vision.model import VisionLanguageModel
from utils.tts import TextToSpeech


def draw_panel(frame, caption, objects, actions, fps_value):
    h, w, _ = frame.shape
    overlay = frame.copy()

    cv2.rectangle(overlay, (0, 0), (int(w * 0.45), int(h * 0.35)), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    lines = [
        f"FPS: {fps_value:5.1f}",
        "Description:",
        *textwrap.wrap(caption, 46),
        f"Objects: {', '.join(objects) if objects else '-'}",
        f"Actions: {', '.join(actions) if actions else '-'}",
    ]

    y = 25
    for line in lines:
        cv2.putText(frame, line, (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 1)
        y += 22


class CaptionSpeaker:
    def __init__(self, analyzer, tts):
        self.analyzer = analyzer
        self.tts = tts
        self.last_caption = ""
        self.running = True

        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while self.running:
            desc = self.analyzer.latest()
            caption = desc.caption

            if (
                caption
                and "Initializing" not in caption
                and caption != self.last_caption
            ):
                print("Queued:", caption)
                self.tts.enqueue(caption)
                self.last_caption = caption

            time.sleep(3)

    def stop(self):
        self.running = False


def main():
    config = load_config()
    print("Model:", config.model_name)
    print("Device:", config.device)
    model = VisionLanguageModel(config.model_name, config.device)
    analyzer = FrameAnalyzer(config, model)
    fps = FPSCounter()

    tts = TextToSpeech()
    speaker = CaptionSpeaker(analyzer, tts)

    try:
        with WebcamStream(
            index=config.camera_index,
            width=config.frame_width,
            height=config.frame_height,
        ) as stream:

            while True:
                frame = stream.read()
                if frame is None:
                    continue

                desc = analyzer.analyze(frame)
                fps_value = fps.tick()

                draw_panel(
                    frame,
                    desc.caption,
                    desc.objects,
                    desc.actions,
                    fps_value,
                )

                # 🔊 MUST be called every loop
                tts.process()

                cv2.imshow(config.window_name, frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break

    finally:
        speaker.stop()
        analyzer.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
