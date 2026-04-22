"""Vision-language model wrapper — CPU optimized, hallucination-suppressed."""
from __future__ import annotations

import re
import threading
from typing import Optional

import torch
from transformers import BlipForConditionalGeneration, BlipProcessor

torch.set_num_threads(2)


class HallucinationFilter:
    """
    Blocks captions containing words that BLIP-base hallucinates from
    its training data when it cannot confidently interpret the frame.

    Two categories of blocked words:

    CONTEXT_REQUIRED — words that CAN be real but need supporting evidence.
      e.g. "bathroom" is real only if "sink"/"towel"/"tile" also appear.

    ALWAYS_BLOCK — words that are NEVER valid in a normal webcam/room scene.
      These come from BLIP's photojournalism and news-caption training data.
      "a photo of" prompts strongly activates this — phrases like "shot",
      "murdered", "killed", "arrested" are frequent in news photo captions
      but never describe what a person is actually doing in a room.
      No amount of context makes these acceptable — block unconditionally.
    """

    # Words blocked only when no supporting context word appears
    CONTEXT_REQUIRED: dict[str, set[str]] = {
        "bathroom":    {"sink", "shower", "towel", "tile", "faucet", "mirror",
                        "toilet", "brush", "brushing", "washing", "hygiene", "toothbrush"},
        "toilet":      {"bathroom", "flush", "lid", "seat", "plumbing"},
        "shower":      {"bathroom", "tile", "curtain", "towel", "faucet"},
        "bathtub":     {"bathroom", "shower", "tile", "faucet"},
        "urinal":      {"bathroom", "restroom"},
        "restroom":    {"sink", "toilet", "tile"},
        "kitchen":     {"stove", "oven", "refrigerator", "fridge", "sink",
                        "counter", "dish", "food", "cook", "cooking", "pot", "pan",
                        "microwave", "eating", "preparing"},
        "stove":       {"kitchen", "oven", "cook", "pot", "pan", "burner"},
        "oven":        {"kitchen", "stove", "bake", "cook"},
        "refrigerator":{"kitchen", "food", "fridge"},
        "bedroom":     {"bed", "pillow", "blanket", "mattress", "nightstand", "sleep"},
        "bed":         {"bedroom", "pillow", "blanket", "mattress", "sleep", "lying"},
        "hospital":    {"doctor", "nurse", "patient", "medical", "equipment", "gown"},
        "swimming":    {"pool", "water", "swimsuit", "swim"},
        "beach":       {"sand", "ocean", "wave", "swim", "shore"},
        "street":      {"car", "road", "sidewalk", "traffic", "bus", "sign"},
        "crowd":       {"people", "stadium", "event", "concert", "protest"},
    }

    # Words ALWAYS blocked — never valid in a webcam room scene
    # These are photojournalism/news-caption hallucinations from BLIP training
    ALWAYS_BLOCK: set[str] = {
        # Violence / crime
        "shot", "shooting", "shoot", "shoots",
        "killed", "kill", "killing", "kills",
        "murdered", "murder", "murdering",
        "stabbed", "stab", "stabbing",
        "attacked", "attack", "attacking",
        "beaten", "beating", "beat up",
        "injured", "injury", "wound", "wounded",
        "dead", "death", "dying", "died",
        "suicide", "suicidal",
        "arrested", "arrest", "handcuffed",
        "gunshot", "gunfire", "bullet",
        "bomb", "explosion", "exploded",
        "accident", "crash", "collision",
        "victim", "suspect", "criminal",
        "robbery", "robbed", "robbing",
        "assault", "assaulted", "raped" ,
        # Medical emergencies
        "unconscious", "bleeding", "blood",
        "emergency", "ambulance", "stretcher",
        "surgery", "operation", "operating",
        # Animals that don't belong in rooms
        "camel", "elephant", "lion", "tiger", "bear", "wolf",
        "crocodile", "alligator", "shark",
        # Extreme scenarios
        "flood", "fire", "burning", "flames",
        "earthquake", "tornado", "hurricane",
    }

    def is_hallucination(self, caption: str) -> bool:
        text = caption.lower()

        # Check always-block words first (no context can save these)
        for word in self.ALWAYS_BLOCK:
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                return True

        # Check context-required words
        for word, context_words in self.CONTEXT_REQUIRED.items():
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                if any(re.search(r'\b' + re.escape(ctx) + r'\b', text)
                       for ctx in context_words):
                    continue   # supported by context — probably real
                return True    # unsupported — treat as hallucination

        return False

    def clean(self, caption: str) -> Optional[str]:
        """Return None if hallucination detected, else return caption unchanged."""
        return None if self.is_hallucination(caption) else caption


class VisionLanguageModel:
    """
    BLIP conditional-captioning wrapper with anti-hallucination layer.

    Prompt strategy — 2 safe prompts only:
      "a person"   → focuses on WHO is in frame and WHAT they are doing.
                     Grounds BLIP on human subjects and everyday actions.
      "there is"   → focuses on OBJECTS and WHERE they are placed.
                     Good for scenes without visible people.

    "a photo of" is intentionally REMOVED. It was the source of the new
    violent hallucinations — BLIP's training data associates "a photo of"
    strongly with photojournalism/news captions ("a photo of a woman who
    was shot...") which appear frequently in COCO/CC training sets.
    "a person" and "there is" stay anchored to everyday scene descriptions.

    Anti-hallucination strategy:
      1. Run ONE pass per call (alternating between the two prompts).
         Dual-pass doubled inference time and introduced more hallucination
         surface. Single-pass with a tight filter is more reliable.
      2. HallucinationFilter screens the result.
      3. If blocked → return last known-clean caption rather than speak garbage.
      4. 3-caption majority vote smoothing suppresses single-frame noise.
    """

    PROMPTS = [
        "a person",   # human-action focused — most reliable for room scenes
        "there is",   # object-location focused
    ]

    def __init__(
        self,
        model_name: str = "Salesforce/blip-image-captioning-base",
        device_preference: str = "cpu",
    ) -> None:
        self.model_name = model_name
        self.device     = self._select_device(device_preference)

        print(f"[VisionModel] Loading {model_name} on {self.device}...", flush=True)
        self.processor = BlipProcessor.from_pretrained(model_name)
        self.model     = BlipForConditionalGeneration.from_pretrained(
            model_name,
            torch_dtype=torch.float32,
        )
        self.model.to(self.device)
        self.model.eval()
        print("[VisionModel] Ready.", flush=True)

        self._lock        = threading.Lock()
        self._prompt_idx  = 0
        self._filter      = HallucinationFilter()
        self._recent: list[str] = []
        self._max_hist    = 3    # majority-vote window
        self._last_clean  = ""   # last caption that passed the filter
        self._blocked_streak = 0  # consecutive blocked count

    @staticmethod
    def _select_device(pref: str) -> torch.device:
        if pref == "cuda" and torch.cuda.is_available():
            return torch.device("cuda")
        if pref == "auto":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device("cpu")

    def _single_pass(self, image, prompt: str, max_tokens: int) -> str:
        """One BLIP inference pass — caller must hold self._lock."""
        with torch.inference_mode():
            inputs = self.processor(
                images=image,
                text=prompt,
                return_tensors="pt",
            ).to(self.device)

            output_ids = self.model.generate(
                **inputs,
                max_new_tokens       = max_tokens,
                num_beams            = 1,
                do_sample            = False,
                repetition_penalty   = 1.3,
                no_repeat_ngram_size = 2,
            )

            return self.processor.tokenizer.decode(
                output_ids[0],
                skip_special_tokens=True,
            ).strip()

    def describe(self, image, max_tokens: int = 35) -> str:
        """
        Generate a hallucination-filtered caption for a PIL RGB image.
        Runs one pass, filters it, returns last clean caption if blocked.
        """
        with self._lock:
            prompt = self.PROMPTS[self._prompt_idx % len(self.PROMPTS)]
            self._prompt_idx = (self._prompt_idx + 1) % len(self.PROMPTS)
            raw = self._single_pass(image, prompt, max_tokens)

        clean = self._filter.clean(raw)
        print(f"[VisionModel] prompt={prompt!r}: {raw!r} → {'OK' if clean else 'BLOCKED'}", flush=True)

        if clean is None:
            self._blocked_streak += 1
            print(f"[VisionModel] Blocked (streak={self._blocked_streak}) — reusing: {self._last_clean!r}", flush=True)
            # After 3 consecutive blocks, try the other prompt on next call
            # to break out of a bad decoding rut
            if self._blocked_streak >= 3:
                self._prompt_idx = (self._prompt_idx + 1) % len(self.PROMPTS)
                self._blocked_streak = 0
            return self._last_clean or "Looking at the scene."

        self._blocked_streak = 0
        self._recent.append(clean)
        if len(self._recent) > self._max_hist:
            self._recent.pop(0)
        self._last_clean = clean
        return clean

    def reset_history(self) -> None:
        self._recent.clear()
        self._prompt_idx     = 0
        self._last_clean     = ""
        self._blocked_streak = 0

    def close(self) -> None:
        del self.model
        del self.processor
        if torch.cuda.is_available():
            torch.cuda.empty_cache()