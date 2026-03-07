"""Vision-language model wrapper — CPU optimized for real-time captioning."""
from __future__ import annotations

import threading
import torch
from transformers import BlipForConditionalGeneration, BlipProcessor

# Limit to 2 threads — leaves headroom for Flask + SAPI on the same machine
torch.set_num_threads(2)


class VisionLanguageModel:
    """
    BLIP conditional-captioning wrapper.

    Prompt strategy:
    - We alternate between two prompts each inference cycle:
        "a person"  → makes BLIP focus on WHO is in the frame and WHAT they're doing
        "there is"  → makes BLIP focus on OBJECTS in the scene

    Alternating gives richer output than a single fixed prompt:
      "a person is holding a cell phone"     (person prompt)
      "there is a cell phone on the table"   (object prompt)
    The FrameAnalyzer picks whichever produced the most informative caption.

    CPU speed settings:
    - num_beams=1 (greedy) — 4x faster than beam search
    - max_new_tokens=35    — keeps captions concise
    - torch.inference_mode — disables gradient engine
    """

    PROMPTS = [
        "a person",    # focuses BLIP on human actions + held objects
        "there is",    # focuses BLIP on objects in the scene
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

        self._lock          = threading.Lock()
        self._prompt_idx    = 0   # alternates between PROMPTS each call
        self._recent: list  = []
        self._max_hist      = 4

    @staticmethod
    def _select_device(pref: str) -> torch.device:
        if pref == "cuda" and torch.cuda.is_available():
            return torch.device("cuda")
        if pref == "auto":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device("cpu")

    def describe(self, image, max_tokens: int = 35) -> str:
        """
        Generate a caption for a PIL RGB image.
        Alternates prompts each call for richer scene descriptions.
        """
        with self._lock:
            # Pick prompt and advance
            prompt = self.PROMPTS[self._prompt_idx % len(self.PROMPTS)]
            self._prompt_idx += 1

            with torch.inference_mode():
                inputs = self.processor(
                    images=image,
                    text=prompt,
                    return_tensors="pt",
                ).to(self.device)

                output_ids = self.model.generate(
                    **inputs,
                    max_new_tokens       = max_tokens,
                    num_beams            = 1,       # greedy — fastest on CPU
                    do_sample            = False,
                    repetition_penalty   = 1.3,
                    no_repeat_ngram_size = 2,
                )

                caption: str = self.processor.tokenizer.decode(
                    output_ids[0],
                    skip_special_tokens=True,
                ).strip()

        self._recent.append(caption)
        if len(self._recent) > self._max_hist:
            self._recent.pop(0)

        return caption

    def reset_history(self) -> None:
        self._recent.clear()
        self._prompt_idx = 0

    def close(self) -> None:
        del self.model
        del self.processor
        if torch.cuda.is_available():
            torch.cuda.empty_cache()