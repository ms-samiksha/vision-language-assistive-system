"""Frame analysis — continuous BLIP worker with latched change events."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple
import threading

import cv2
from PIL import Image

from utils.config import AppConfig
from vision.model import VisionLanguageModel


# ─── Scene change detector ────────────────────────────────────────────────────
class SceneChangeDetector:
    """
    Only fires when objects OR actions meaningfully change.

    Key rule: two captions describing the same objects+actions are NOT a change,
    even if the wording is different. Examples that are NOT changes:
      "Someone with glasses on their head"  vs  "There is a woman with glasses on"
      "A person is sitting"  vs  "Someone sitting on the couch"
    Both extract objects={"glasses","woman/person"} actions={"sitting"} — same sets.

    Also requires a minimum 6s gap between events so rapid BLIP noise
    can't fire faster than SAPI can speak.
    """

    MIN_GAP_S = 6.0   # minimum seconds between announced events

    def __init__(self) -> None:
        self._last_objects: Set[str] = set()
        self._last_actions: Set[str] = set()
        self._first     = True
        self._last_fire = 0.0

    # Normalize object names so "woman"/"man"/"someone" don't cause false changes
    _PERSON_WORDS = {"woman", "man", "boy", "girl", "child", "person", "people", "someone"}

    def _normalize_objs(self, objects: List[str]) -> Set[str]:
        """Strip person-gender words — treat all humans as same category."""
        result = set()
        for o in objects:
            if o in self._PERSON_WORDS:
                result.add("person")   # collapse all person variants
            else:
                result.add(o)
        return result

    def check(self, objects: List[str], actions: List[str]) -> bool:
        now     = time.time()
        obj_set = self._normalize_objs(objects)
        act_set = set(actions)

        if self._first:
            self._first        = False
            self._last_objects = obj_set
            self._last_actions = act_set
            self._last_fire    = now
            return True

        # Enforce minimum gap — don't fire faster than SAPI can speak
        if now - self._last_fire < self.MIN_GAP_S:
            return False

        changed = (obj_set != self._last_objects) or (act_set != self._last_actions)
        if changed:
            self._last_objects = obj_set
            self._last_actions = act_set
            self._last_fire    = now
        return changed

    def reset(self) -> None:
        self._last_objects = set()
        self._last_actions = set()
        self._first        = True
        self._last_fire    = 0.0


# ─── Object/action extractor ──────────────────────────────────────────────────
class ObjectActionExtractor:
    OBJECTS = {
        "person", "man", "woman", "boy", "girl", "child", "people",
        "phone", "cell phone", "mobile", "smartphone", "cell",
        "laptop", "computer", "keyboard", "mouse", "tablet",
        "bottle", "water bottle", "cup", "mug", "glass", "plate", "bowl",
        "bag", "backpack", "purse", "wallet", "keys", "key",
        "watch", "clock", "glasses", "hat", "cap",
        "earphones", "headphones",
        "pen", "pencil", "notebook", "book",
        "chair", "sofa", "couch", "desk", "table", "bed",
        "car", "bike", "bicycle",
        "dog", "cat",
        "food", "apple", "banana", "sandwich",
        "door", "window", "remote", "controller", "tv",
    }
    ACTIONS = {
        "holding", "carrying", "using", "typing", "looking",
        "sitting", "standing", "drinking", "eating", "reading",
        "pointing", "showing", "walking", "running", "waving",
        "talking", "working", "lying", "sleeping", "wearing", "watching",
        "taking", "playing",
    }

    def extract(self, caption: str) -> Tuple[List[str], List[str]]:
        text = caption.lower()
        objects = sorted({o for o in self.OBJECTS if re.search(r'\b' + re.escape(o) + r'\b', text)})
        actions = sorted({a for a in self.ACTIONS if re.search(r'\b' + re.escape(a) + r'\b', text)})
        return objects, actions


# ─── Formatter ────────────────────────────────────────────────────────────────
class AccessibilityFormatter:
    STRIP_PREFIXES = [
        "a photo of a ", "a photo of an ", "a photo of ",
        "an image of a ", "an image of an ", "an image of ",
        "a picture of a ", "a picture of an ", "a picture of ",
        "this image shows ", "the image shows ",
        "a photograph of ", "this photo shows ",
        "a close up of a ", "a close-up of a ",
        "a close up of ", "a close-up of ",
    ]
    REPLACEMENTS = [
        (r"\bappears to be\b",  "is"),
        (r"\blooks like\b",     "is"),
        (r"\bmight be\b",       "is"),
        (r"\bpossibly\b",       ""),
        (r"\bmaybe\b",          ""),
        (r"\bthe\s+image\b",    "the scene"),
        (r"\bthe\s+photo\b",    "the scene"),
        (r"\bin\s+the\s+background\b", "nearby"),
        (r"\bhas (?:her|his|a|an)\s+cell(?:\s+phone)?\b", "holding a phone"),
        (r"\bhas (?:her|his|a|an)\s+phone\b",  "holding a phone"),
        (r"\bhas (?:her|his|a|an)\s+mobile\b", "holding a phone"),
        (r"\bhas (?:her|his|a|an)\s+bottle\b", "holding a bottle"),
        (r"\bhas (?:her|his|a|an)\s+cup\b",    "holding a cup"),
        (r"\bhas (?:her|his|a|an)\s+bag\b",    "carrying a bag"),
        (r"\bhas (?:her|his|a|an)\s+book\b",   "reading a book"),
        (r"\btaking (?:a |her |his )?selfie\b", "taking a selfie"),
        (r"\btaking (?:a |her |his )?picture\b","taking a picture"),
        (r"^A person is ", "Someone is "),
        (r"^Someoneis\b", "Someone is"),   # fix missing space
    ]
    SUBJECT_MAP = {
        "a person": "Someone", "the person": "The person",
        "a man": "A man", "a woman": "A woman",
        "a girl": "A girl", "a boy": "A boy",
        "a child": "A child", "people": "People",
    }
    _IS_PATTERN = re.compile(
        r'^(Someone|A man|A woman|A girl|A boy|A child|People|The person)\s+'
        r'(holding|carrying|using|typing|looking|sitting|standing|drinking|'
        r'eating|reading|pointing|walking|running|waving|talking|working|'
        r'lying|wearing|watching|taking|playing)\b', re.IGNORECASE,
    )

    def format(self, raw: str) -> str:
        text = raw.strip()
        if not text or text in ("Initializing...", "Resetting..."):
            return text
        tl = text.lower()
        for pfx in sorted(self.STRIP_PREFIXES, key=len, reverse=True):
            if tl.startswith(pfx):
                text = text[len(pfx):]
                tl = text.lower()
                break
        for pat, rep in self.REPLACEMENTS:
            text = re.sub(pat, rep, text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        for rs, ns in self.SUBJECT_MAP.items():
            if tl.startswith(rs):
                text = ns + text[len(rs):]
                tl = text.lower()
                break
        text = self._IS_PATTERN.sub(lambda m: f"{m.group(1)} is {m.group(2)}", text)
        if text:
            text = text[0].upper() + text[1:]
        if text and not text.endswith((".", "!", "?")):
            text += "."
        return text


# ─── Frame description ────────────────────────────────────────────────────────
@dataclass
class FrameDescription:
    caption:      str
    objects:      List[str]
    actions:      List[str]
    timestamp:    float
    is_new_event: bool = False


# ─── Frame analyzer ───────────────────────────────────────────────────────────
class FrameAnalyzer:
    """
    Runs BLIP continuously in a background thread.

    KEY FIX — event latching:
    When BLIP detects a scene change (is_new_event=True), that flag stays True
    until app.py explicitly consumes it by calling consume_event().
    This prevents the race condition where the HTTP poll happens 50ms after
    the event but the next BLIP run has already overwritten is_new_event=False.
    """

    def __init__(self, config: AppConfig, model: VisionLanguageModel,
                 webcam=None) -> None:
        self.config    = config
        self.model     = model
        self.extractor = ObjectActionExtractor()
        self.formatter = AccessibilityFormatter()
        self.detector  = SceneChangeDetector()

        self._state = FrameDescription(
            caption="Initializing...", objects=[], actions=[],
            timestamp=time.time(), is_new_event=False,
        )
        self._pending_event: Optional[FrameDescription] = None  # latched new event
        self._lock  = threading.Lock()

        self._webcam      = webcam
        self._webcam_lock = threading.Lock()
        self._running     = True

        self._worker = threading.Thread(
            target=self._loop, daemon=True, name="BLIPWorker"
        )
        self._worker.start()

    def set_webcam(self, webcam) -> None:
        with self._webcam_lock:
            self._webcam = webcam

    def _downscale(self, frame):
        target = max(160, self.config.inference_short_side)
        h, w   = frame.shape[:2]
        short  = min(h, w)
        if short <= target:
            return frame
        scale = target / short
        return cv2.resize(frame, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_AREA)

    def _loop(self) -> None:
        while self._running:
            # Get webcam
            with self._webcam_lock:
                cam = self._webcam
            if cam is None:
                time.sleep(0.2)
                continue

            frame = cam.read()
            if frame is None:
                time.sleep(0.1)
                continue

            try:
                pil = Image.fromarray(cv2.cvtColor(
                    self._downscale(frame), cv2.COLOR_BGR2RGB))
                raw      = self.model.describe(pil, max_tokens=self.config.max_caption_tokens)
                caption  = self.formatter.format(raw)
                objs, acts = self.extractor.extract(caption)
                is_new   = self.detector.check(objs, acts)

                desc = FrameDescription(
                    caption=caption, objects=objs, actions=acts,
                    timestamp=time.time(), is_new_event=is_new,
                )

                with self._lock:
                    self._state = desc
                    if is_new:
                        # Only latch if no pending event yet, or this one has
                        # meaningfully different objects (not just different wording)
                        if self._pending_event is None:
                            self._pending_event = desc

                tag = "NEW EVENT" if is_new else "no change"
                print(f"[BLIP] {tag}: {caption}", flush=True)

            except Exception as e:
                print(f"[BLIP] Error: {e}", flush=True)

            # Wait 5s between inferences — BLIP takes ~2-3s on CPU,
            # so effective rate is one caption every ~7-8s which is
            # enough to catch real scene changes without flooding SAPI.
            time.sleep(5.0)

    def latest(self) -> FrameDescription:
        """Return current state (no side effects)."""
        with self._lock:
            return self._state

    def consume_event(self) -> Optional[FrameDescription]:
        """
        Return and clear the pending new-event, or None if nothing new.
        Called by app.py's /api/caption on each poll.
        """
        with self._lock:
            ev = self._pending_event
            self._pending_event = None
            return ev

    def analyze(self, frame) -> FrameDescription:
        """Backward-compat shim."""
        return self.latest()

    def reset(self) -> None:
        self.detector.reset()
        self.model.reset_history()
        with self._lock:
            self._pending_event = None
            self._state = FrameDescription(
                caption="Resetting...", objects=[], actions=[],
                timestamp=time.time(), is_new_event=False,
            )

    def close(self) -> None:
        self._running = False
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)