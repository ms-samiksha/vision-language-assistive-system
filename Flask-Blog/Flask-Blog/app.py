"""See & Tell — Flask backend. Mobile-friendly (browser TTS)."""
import os, json, re, time, datetime, threading, queue, sys, uuid, atexit
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from camera.webcam   import WebcamStream
from utils.config    import AppConfig, load_config
from vision.inference import FrameAnalyzer, FrameDescription
from vision.model    import VisionLanguageModel

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_key")

# ── Guest user (no login needed) ─────────────────────────────────────────────
GUEST_USERNAME = "guest"
GUEST_USER = {
    "username":                "guest",
    "vision_assistance_level": "Fully Blind",
    "auto_speak":              True,
    "speech_rate":             160,
    "important_objects":       [
        "phone", "cell", "keys", "wallet", "glasses",
        "bag", "water bottle", "bottle", "laptop",
    ],
}

# ── Vision pipeline state ─────────────────────────────────────────────────────
vision_initialized : bool          = False
vision_running     : bool          = False
analyzer           : FrameAnalyzer = None
webcam_stream      : WebcamStream  = None
vision_config      : AppConfig     = None
vision_error       : str           = None
vision_lock        = threading.Lock()


# ── Mobile camera stream ───────────────────────────────────────────────────────
class MobileStream:
    """
    Drop-in replacement for WebcamStream that receives JPEG frames pushed
    from the phone browser via POST /api/camera/frame.

    The phone uses getUserMedia() to capture from its camera, draws each
    frame onto a canvas, calls canvas.toBlob('image/jpeg'), and POSTs the
    blob to /api/camera/frame every 200ms (~5 fps — enough for BLIP).

    This class exposes the same read() / is_ready() / start() / stop()
    interface as WebcamStream so FrameAnalyzer needs zero changes.
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._frame = None          # latest decoded numpy frame (BGR)
        self._jpeg  = None          # latest raw JPEG bytes for the stream endpoint
        self._ready = False

    def start(self) -> "MobileStream":
        return self          # nothing to start — frames arrive via HTTP POST

    def stop(self) -> None:
        pass

    def push_jpeg(self, data: bytes) -> None:
        """Called by the /api/camera/frame route with raw JPEG bytes."""
        import numpy as np
        import cv2 as _cv
        arr   = np.frombuffer(data, dtype=np.uint8)
        frame = _cv.imdecode(arr, _cv.IMREAD_COLOR)
        if frame is None:
            return
        with self._lock:
            self._frame = frame
            self._jpeg  = data
            self._ready = True

    def read(self):
        """Return a copy of the latest frame (BGR numpy array), or None."""
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def read_jpeg(self) -> bytes | None:
        """Return latest JPEG bytes for the MJPEG stream endpoint."""
        with self._lock:
            return self._jpeg

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    # Context-manager shim (WebcamStream supports this)
    def __enter__(self):  return self.start()
    def __exit__(self, *_): self.stop()


# Global mobile stream instance — created once, reused across start/stop cycles
_mobile_stream: MobileStream = MobileStream()

# Caption tracking — only speak/show on new events
last_sent_caption     : str   = ""
last_caption_update_t : float = 0.0
caption_lock          = threading.Lock()

# Search mode — pauses captions
searching_mode      : bool = False
searching_mode_lock = threading.Lock()

# File lock — prevents concurrent threads racing on memory.json reads/writes
memory_file_lock = threading.Lock()

# ── Logging ───────────────────────────────────────────────────────────────────
def log(cat: str, msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{cat}] {msg}", flush=True)


# ── Speech: browser TTS only ──────────────────────────────────────────────────
# SAPI (Windows-only) removed. All speech is handled by window.speechSynthesis
# in app.js — works on desktop browsers AND mobile browsers over Wi-Fi.
# These stubs keep callers working without any changes to call sites.

def clear_speech_queue() -> None:
    pass  # no-op — browser handles TTS, nothing to clear server-side


# ── Voice command processor ───────────────────────────────────────────────────
voice_cmd_queue   : queue.Queue = queue.Queue()
voice_cmd_results : dict        = {}
voice_cmd_lock    = threading.Lock()

def _voice_worker() -> None:
    log("VOICE", "Processor started")
    while True:
        try:
            task = voice_cmd_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if task is None:
            break
        cmd_id, command, user_data = task
        log("VOICE", f"Processing: '{command}'")
        try:
            result = _process_command(command, user_data)
            result["_ts"] = time.time()
        except Exception as e:
            import traceback
            log("VOICE_ERR", traceback.format_exc())
            result = {"status": "error", "response": f"Error: {e}", "type": "error", "_ts": time.time()}
        with voice_cmd_lock:
            voice_cmd_results[cmd_id] = result
        # Speech is handled by browser TTS — response is sent via JSON to app.js
        voice_cmd_queue.task_done()

threading.Thread(target=_voice_worker, daemon=True, name="VoiceWorker").start()


# ── Data storage ──────────────────────────────────────────────────────────────
DATA_DIR    = "data"
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
os.makedirs(DATA_DIR, exist_ok=True)

def _load(fp):
    try:
        with open(fp) as f: return json.load(f)
    except: return []

def _save(fp, data):
    with open(fp, "w") as f: json.dump(data, f, indent=2)

def _normalize(t: str) -> str:
    t = t.lower().strip()
    for s, r in [("ies","y"),("es",""),("s","")]:
        if t.endswith(s) and len(t) > len(s)+1:
            return t[:-len(s)] + r
    return t

def upsert_memory(username: str, obj: str, description: str, location: str = None) -> None:
    """
    Appends a new sighting record under a file lock so concurrent threads
    (one per object from _auto_save) never race and overwrite each other.

    Dedup guard: skip if this exact (object + description) was already saved
    within the last 60 seconds — prevents the 3-second poll from creating
    duplicate rows for the exact same BLIP caption.
    """
    now_dt   = datetime.datetime.now()
    now_str  = now_dt.strftime("%I:%M %p")
    obj_lower = obj.lower()
    obj_norm  = _normalize(obj_lower)

    with memory_file_lock:
        memories = _load(MEMORY_FILE)

        # Dedup: walk backwards, find most recent entry for THIS object only
        for m in reversed(memories):
            if m.get("user") != username:
                continue
            mn = _normalize(m.get("object","").lower())
            if mn != obj_norm and obj_lower not in m.get("object","").lower():
                continue
            # Found most recent entry for this object — check dedup window
            if m.get("description","") == description:
                try:
                    stored = datetime.datetime.strptime(m["timestamp"], "%I:%M %p").replace(
                        year=now_dt.year, month=now_dt.month, day=now_dt.day)
                    if abs((now_dt - stored).total_seconds()) < 60:
                        log("MEMORY", f"Dedup skip '{obj}' (same caption within 60s)")
                        return
                except Exception:
                    pass
            break  # only check the single most-recent entry for this object

        entry = {
            "object":      obj,
            "description": description,
            "timestamp":   now_str,
            "user":        username,
        }
        if location:
            entry["location"] = location

        memories.append(entry)
        _save(MEMORY_FILE, memories)
        log("MEMORY", f"Appended '{obj}' @ {now_str} loc={location!r}")


# ── Core voice command logic ──────────────────────────────────────────────────
def _process_command(command: str, user_data: dict) -> dict:
    global searching_mode, vision_running

    # Strip trailing punctuation Chrome sometimes appends ("search." "stop.")
    cmd = command.lower().strip().rstrip(".,!?")
    username = user_data.get("username", GUEST_USERNAME)

        # STOP — bare 'stop' OR any stop phrase
    STOP_KWS = ["stop vision","stop captions","stop camera","pause captions","pause vision"]
    if cmd.strip() in ("stop","pause") or any(kw in cmd for kw in STOP_KWS):
        vision_running = False
        return {"response": "Captions stopped.", "type": "stop"}

    # SEARCH — bare 'search' OR any search phrase (checked AFTER exit above)
    SEARCH_KWS = ["search mode","begin search","start search","find mode"]
    if cmd.strip() in ("search","find") or any(kw in cmd for kw in SEARCH_KWS):
        with searching_mode_lock:
            searching_mode = True
        log("VOICE", "Search mode ON")
        return {"response": "Search mode on. What are you looking for?",
                "type": "search_start"}

    # EXIT SEARCH — bare 'done' OR any exit phrase
    EXIT_KWS2 = ["done searching","exit search","resume captions","stop searching","cancel search"]
    if cmd.strip() in ("done","resume") or any(kw in cmd for kw in EXIT_KWS2):
        with searching_mode_lock:
            searching_mode = False
        return {"response": "Resuming live captions.", "type": "search_end"}

    # START — if vision already initializing/running, don't restart
    START_KWS = ["start captions","start vision","begin captions","begin vision"]
    if cmd.strip() in ("start","begin") or any(kw in cmd for kw in START_KWS):
        if vision_running:
            return {"response": "Already running.", "type": "already_running"}
        vision_running = True
        if vision_initialized:
            # Model already loaded — just flip the running flag, no need to reload
            return {"response": "Captions resumed.", "type": "start"}
        threading.Thread(target=_init_vision, daemon=True).start()
        return {"response": "Starting up. Please wait.", "type": "start"}

    # ─── Find / where is ─────────────────────────────────────────────────────
    user     = GUEST_USER
    imp_objs = user.get("important_objects", [])
    common   = ["phone","cell","keys","key","wallet","glasses","laptop","bottle",
                "water bottle","bag","backpack","remote","book","tablet","watch"]
    all_objs = list(imp_objs) + [o for o in common if o not in imp_objs]

    # Normalise common speech-recognition aliases before matching
    # e.g. Chrome often returns "cell phone" when user says "cell"
    QUERY_ALIASES = {
        "cell phone":   "phone",
        "mobile phone": "phone",
        "mobile":       "phone",
        "cellular":     "cell",
        "spectacles":   "glasses",
        "eyeglasses":   "glasses",
        "specs":        "glasses",
        "handbag":      "bag",
        "purse":        "bag",
        "knapsack":     "backpack",
        "laptop computer": "laptop",
    }
    for alias, canonical in QUERY_ALIASES.items():
        if alias in cmd:
            cmd = cmd.replace(alias, canonical)

    WHERE_KWS = ["where is","where's","where are","where did","where was",
                 "find my","find the","locate my","lost my","i lost"]
    with searching_mode_lock:
        in_search = searching_mode

    if any(kw in cmd for kw in WHERE_KWS) or in_search:
        memories  = _load(MEMORY_FILE)
        user_mems = [m for m in memories if m.get("user") == username]

        # ── Extract the query term — use only the LAST meaningful word/phrase ──
        # Chrome SpeechRecognition with continuous=True can bleed previous
        # recognised words into the current result (e.g. "cell bottle" when
        # the user said "cell" before and now says "bottle").
        # Strategy: strip known filler words, then prefer the LAST word that
        # matches a known object over earlier words in the transcript.
        FILLER = {"my","the","a","an","where","is","are","find","locate",
                  "i","lost","have","you","seen","for","looking","please","its"}
        query_words = [w for w in cmd.split() if w not in FILLER and len(w) > 1]
        # query_words is ordered as spoken — last word is most recent intent
        # Try to match longest first (so "water bottle" > "bottle") then by
        # recency (last word wins ties)

        found_obj = None

        # ── Step 1a: exact regex against known object list, longest object first ─
        for obj in sorted(all_objs, key=len, reverse=True):
            if re.search(r'\b' + re.escape(obj) + r'\b', cmd):
                found_obj = obj; break

        # ── Step 1b: if multiple words matched, prefer the LAST one spoken ──────
        if not found_obj and query_words:
            # Try each word from the END of the transcript (most recent first)
            for word in reversed(query_words):
                for obj in sorted(all_objs, key=len, reverse=True):
                    if re.search(r'\b' + re.escape(obj) + r'\b', word):
                        found_obj = obj; break
                    if _normalize(obj) == _normalize(word):
                        found_obj = obj; break
                if found_obj:
                    break

        # ── Step 2: fuzzy match against actual memory objects ─────────────────
        if not found_obj and user_mems:
            # Try last word first, then full query
            candidates = list(reversed(query_words)) + [cmd]
            for candidate in candidates:
                cn = _normalize(candidate)
                for m in reversed(user_mems):
                    mem_obj  = m.get("object","").lower()
                    mem_norm = _normalize(mem_obj)
                    # Exact normalized match or direct word match — no substring
                    if mem_norm == cn or mem_obj == candidate:
                        found_obj = m.get("object",""); break
                    # Single-word candidate that exactly matches object
                    if candidate in mem_obj.split() or mem_obj in candidate.split():
                        found_obj = m.get("object",""); break
                if found_obj:
                    break

        # ── Step 3: look up found_obj in memory and return result ─────────────
        if found_obj:
            fo_norm = _normalize(found_obj.lower())
            fo_lower = found_obj.lower()
            # Find newest memory entry that exactly matches this object
            matched_entry = None
            for m in reversed(user_mems):
                mn = _normalize(m.get("object","").lower())
                ml = m.get("object","").lower()
                # Exact normalized match only — no substring to avoid false positives
                if mn == fo_norm or ml == fo_lower:
                    matched_entry = m; break
            if matched_entry:
                obj_name = matched_entry.get("object","")
                on_norm  = _normalize(obj_name.lower())
                sighting_count = sum(
                    1 for x in user_mems
                    if _normalize(x.get("object","").lower()) == on_norm
                )
                loc = matched_entry.get("location")
                if loc:
                    resp = (f"Your {obj_name} was last seen {loc} "
                            f"at {matched_entry['timestamp']}.")
                else:
                    resp = (f"Your {obj_name} was last seen at {matched_entry['timestamp']}. "
                            f"It was: {matched_entry['description']}")
                return {"response": resp, "type": "location"}
            # Object name known but no memory entry found
            return {"response": f"I haven't seen your {found_obj} recently. Try another object?",
                    "type": "not_found"}

        # Nothing matched — stay in search mode, list what we know
        if user_mems:
            seen = []
            seen_norm = set()
            for m in reversed(user_mems):
                n = _normalize(m.get("object","").lower())
                if n not in seen_norm:
                    seen.append(m.get("object","?"))
                    seen_norm.add(n)
                if len(seen) >= 5: break
            return {"response": f"I didn't catch that.  Say the object name clearly.",
                    "type": "ask"}
        return {"response": "No items in memory yet. Start captions first so I can learn the scene.",
                "type": "empty"}

    # ─── Save ─────────────────────────────────────────────────────────────────
    if any(kw in cmd for kw in ["save","remember","memorize"]):
        desc = analyzer.latest() if analyzer else None
        if desc and desc.objects:
            loc = getattr(desc, "location", None)
            # Save ALL objects visible right now, not just the first one
            saved_names = []
            for obj in desc.objects:
                obj_lower = obj.lower()
                if obj_lower not in ("person","people","someone","man","woman","child"):
                    upsert_memory(username, obj, desc.caption, loc)
                    saved_names.append(obj)
            if saved_names:
                return {"response": f"Saved {', '.join(saved_names)}.", "type": "saved"}
        return {"response": "Nothing clear to save right now.", "type": "no_object"}

    # ─── Describe ─────────────────────────────────────────────────────────────
    if any(kw in cmd for kw in ["describe","what do you see","tell me","what is that","what's there"]):
        desc = analyzer.latest() if analyzer else None
        if desc and desc.caption and "Initializing" not in desc.caption:
            r = desc.caption
            if desc.objects:
                r += f" I can see: {', '.join(desc.objects)}."
            return {"response": r, "type": "description"}
        return {"response": "Still starting up.", "type": "initializing"}

    # ─── Memory list / clear ──────────────────────────────────────────────────
    if any(kw in cmd for kw in ["list memory","what do you remember","show memory"]):
        mems = [m for m in _load(MEMORY_FILE) if m.get("user") == username]
        if mems:
            items = [m.get("object","?") for m in reversed(mems)]
            return {"response": f"I remember: {', '.join(items)}.", "type": "list"}
        return {"response": "Nothing saved yet.", "type": "empty"}

    if any(kw in cmd for kw in ["clear memory","forget everything","reset memory"]):
        remaining = [m for m in _load(MEMORY_FILE) if m.get("user") != username]
        _save(MEMORY_FILE, remaining)
        return {"response": "Memory cleared.", "type": "cleared"}

    return {"response": 'Commands: start, stop, search, done searching, describe, where is my keys.', "type": "help"}


# ── Vision pipeline init ──────────────────────────────────────────────────────
def _init_vision() -> bool:
    global vision_initialized, analyzer, webcam_stream, vision_config, vision_error, vision_running

    with vision_lock:
        if vision_initialized:
            vision_running = True
            return True
        try:
            log("VISION", "Loading model...")
            vision_config = load_config()

            model = VisionLanguageModel(vision_config.model_name, vision_config.device)

            # Use the mobile stream — phone pushes frames via POST /api/camera/frame
            # No OpenCV VideoCapture needed; MobileStream has the same interface.
            global _mobile_stream
            _mobile_stream = MobileStream()
            webcam_stream  = _mobile_stream   # keep reference for MJPEG endpoint
            log("VISION", "Mobile stream ready — waiting for phone frames")

            # Create analyzer and inject the mobile stream
            analyzer = FrameAnalyzer(vision_config, model, webcam=_mobile_stream)

            vision_initialized = True
            vision_running     = True
            vision_error       = None
            log("VISION", "Pipeline ready!")
            return True
        except Exception as e:
            import traceback
            vision_error       = traceback.format_exc()
            vision_initialized = False
            vision_running     = False
            log("VISION_ERR", str(e))
            return False


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", user=GUEST_USER)

@app.route("/memory")
def memory():
    mems = [m for m in _load(MEMORY_FILE) if m.get("user") == GUEST_USERNAME]
    return render_template("memory.html", memories=reversed(mems), user=GUEST_USER)

@app.route("/find", methods=["GET","POST"])
def find():
    result = None
    if request.method == "POST":
        query = request.form["object"].lower().strip()
        for m in reversed(_load(MEMORY_FILE)):
            if m.get("user") == GUEST_USERNAME and (
                query in m["object"].lower() or
                _normalize(query) == _normalize(m["object"])
            ):
                loc = m.get("location")
                if loc:
                    result = f"Your {m['object']} was last seen {loc} at {m['timestamp']}."
                else:
                    result = (f"Your {m['object']} was last seen at {m['timestamp']}. "
                              f"It was: {m['description']}")
                break
        if result is None:
            result = f"I haven't seen your {query} recently."
    return render_template("find.html", result=result, user=GUEST_USER)

@app.route("/settings", methods=["GET","POST"])
def settings():
    global GUEST_USER
    if request.method == "POST":
        GUEST_USER = dict(GUEST_USER)
        GUEST_USER["auto_speak"]  = "auto_speak" in request.form
        GUEST_USER["speech_rate"] = int(request.form.get("speech_rate", 160))
        GUEST_USER["important_objects"] = [
            x.strip().lower()
            for x in request.form.get("important_objects","").split(",")
            if x.strip()
        ]
        return redirect(url_for("dashboard"))
    return render_template("settings.html", user=GUEST_USER)

@app.route("/logout")
def logout():
    return redirect(url_for("dashboard"))

@app.route("/manifest.json")
def manifest():
    from flask import Response
    import json
    manifest_data = {
        "name": "See & Tell",
        "short_name": "See & Tell",
        "description": "Real-time vision assistant for visually impaired users",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0d14",
        "theme_color": "#0b0d14",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }
    return Response(
        json.dumps(manifest_data),
        mimetype="application/manifest+json"
    )


# ── Camera start / stop ───────────────────────────────────────────────────────
@app.route("/api/camera/start", methods=["POST"])
def api_camera_start():
    global vision_running
    if vision_initialized:
        vision_running = True
        return jsonify({"status": "resumed"})
    vision_running = True
    threading.Thread(target=_init_vision, daemon=True, name="VisionInit").start()
    return jsonify({"status": "starting",
                    "message": "Loading AI model. Please wait..."})

@app.route("/api/camera/stop", methods=["POST"])
def api_camera_stop():
    global vision_running
    vision_running = False
    clear_speech_queue()
    return jsonify({"status": "stopped"})


# ── MJPEG stream ──────────────────────────────────────────────────────────────
_PLACEHOLDER_JPEG = bytes([
    0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,0x01,0x00,
    0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,0x00,0x08,0x06,0x06,
    0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,0x09,0x08,0x0A,0x0C,0x14,0x0D,
    0x0C,0x0B,0x0B,0x0C,0x19,0x12,0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,
    0x1A,0x1C,0x1C,0x20,0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,
    0x37,0x29,0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
    0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,0x00,0x01,
    0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,0x01,0x05,0x01,0x01,
    0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x01,0x02,
    0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A,0x0B,0xFF,0xDA,0x00,0x08,0x01,
    0x01,0x00,0x00,0x3F,0x00,0xFB,0xD4,0xFF,0xD9,
])

@app.route("/api/camera/stream")
def api_camera_stream():
    """MJPEG stream — serves whatever the phone last pushed."""
    def generate():
        while True:
            if not vision_running or webcam_stream is None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _PLACEHOLDER_JPEG + b"\r\n"
                time.sleep(0.25)
                continue
            # MobileStream provides read_jpeg() — use raw bytes directly (no re-encode)
            if hasattr(webcam_stream, "read_jpeg"):
                jpeg = webcam_stream.read_jpeg()
                if jpeg:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                else:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _PLACEHOLDER_JPEG + b"\r\n"
                time.sleep(0.1)   # 10 fps display
            else:
                import cv2 as _cv2
                frame = webcam_stream.read()
                if frame is None:
                    time.sleep(0.05)
                    continue
                ok, buf = _cv2.imencode(".jpg", frame, [_cv2.IMWRITE_JPEG_QUALITY, 72])
                if ok:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                time.sleep(0.05)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ── Caption API ───────────────────────────────────────────────────────────────
@app.route("/api/caption")
def api_caption():
    global last_sent_caption, last_caption_update_t

    if not vision_running:
        return jsonify({"status":"STOPPED",
                        "caption":'Click ▶ Start (or say "start AI") to begin.',
                        "objects":[], "actions":[],
                        "updated_at": _ts()})

    with searching_mode_lock:
        if searching_mode:
            return jsonify({"status":"PAUSED",
                            "caption":'Search mode active. Say what you\'re looking for.',
                            "objects":[], "actions":[],
                            "updated_at": _ts()})

    if not vision_initialized:
        return jsonify({"status":"LOADING",
                        "caption":"Loading AI model, please wait...",
                        "objects":[], "actions":[],
                        "updated_at": _ts()})

    if vision_error:
        return jsonify({"status":"ERROR",
                        "caption": vision_error.splitlines()[0],
                        "objects":[], "actions":[],
                        "updated_at": _ts()}), 500

    desc = analyzer.latest()

    if desc.caption in ("Initializing...", "Resetting..."):
        return jsonify({"status":"LOADING", "caption": desc.caption,
                        "objects":[], "actions":[], "updated_at": _ts()})

    now        = time.time()
    status     = "LIVE" if (now - desc.timestamp) < 12 else "SLOW"
    auto_speak = GUEST_USER.get("auto_speak", True)

    # consume_event() returns the latched new event and clears it atomically.
    # This prevents the race where BLIP overwrites is_new_event before the poll.
    new_event = analyzer.consume_event()

    should_announce = False      # defined here so scope is unambiguous
    speak_caption   = None       # text for browser TTS, None = don't speak

    with caption_lock:
        if (new_event is not None
                and new_event.caption != last_sent_caption
                and "Initializing" not in new_event.caption):
            should_announce   = True
            last_sent_caption = new_event.caption
            last_caption_update_t = now
            log("CAPTION", f"ANNOUNCE: {new_event.caption}")
            if auto_speak:
                speak_caption = new_event.caption   # browser will speak this
            _auto_save(new_event)
            desc = new_event

    return jsonify({
        "status":     status,
        "caption":    last_sent_caption or desc.caption,
        "objects":    desc.objects,
        "actions":    desc.actions,
        "location":   getattr(desc, "location", None),
        "updated_at": _ts(),
        "is_new":     should_announce,
        "speak_text": speak_caption,    # non-null only when auto_speak=True + new caption
    })

def _ts():
    return datetime.datetime.now().strftime("%I:%M %p")

def _auto_save(desc: FrameDescription) -> None:
    """
    Save ALL extracted objects to memory on every caption.
    Called in a single background thread — objects are saved sequentially
    under memory_file_lock so there is no race between parallel writers.
    """
    if not desc.objects:
        return
    location = getattr(desc, "location", None)

    def _save_all():
        saved = set()
        for obj in desc.objects:
            obj_lower = obj.lower()
            if obj_lower in ("person", "people", "someone", "man", "woman", "child"):
                continue
            if obj_lower not in saved:
                saved.add(obj_lower)
                upsert_memory(GUEST_USERNAME, obj, desc.caption, location)

    threading.Thread(target=_save_all, daemon=True).start()




@app.route("/api/camera/frame", methods=["POST"])
def api_camera_frame():
    """
    Receive a JPEG frame from the phone camera.
    The browser posts raw JPEG bytes from canvas.toBlob().
    Called ~5 times/second from the phone while captions are running.
    """
    global _mobile_stream
    data = request.get_data()   # raw bytes
    if not data:
        return jsonify({"status": "empty"}), 400
    _mobile_stream.push_jpeg(data)
    return jsonify({"status": "ok", "size": len(data)})

# ── Voice command routes ──────────────────────────────────────────────────────
def _queue_command(command: str) -> str:
    cmd_id = str(uuid.uuid4())
    voice_cmd_queue.put((cmd_id, command, {
        "username":   GUEST_USERNAME,
        "auto_speak": GUEST_USER.get("auto_speak", True),
    }))
    return cmd_id

@app.route("/api/voice/command", methods=["POST"])
def api_voice_command():
    data    = request.json or {}
    command = data.get("command","").strip()
    if not command:
        return jsonify({"status":"error","message":"Empty"}), 400
    cmd_id  = _queue_command(command)
    # Wait up to 6s for result
    deadline = time.time() + 6.0
    while time.time() < deadline:
        with voice_cmd_lock:
            if cmd_id in voice_cmd_results:
                r = voice_cmd_results.pop(cmd_id)
                return jsonify({"status":"success","response":r["response"],"type":r["type"]})
        time.sleep(0.05)
    return jsonify({"status":"processing","command_id":cmd_id})

@app.route("/api/voice/submit", methods=["POST"])
def api_voice_submit():
    data    = request.json or {}
    command = data.get("command","").strip()
    if not command:
        return jsonify({"status":"error"}), 400
    cmd_id = _queue_command(command)
    return jsonify({"status":"queued","command_id":cmd_id})

@app.route("/api/voice/result/<cmd_id>")
def api_voice_result(cmd_id):
    with voice_cmd_lock:
        if cmd_id in voice_cmd_results:
            r = voice_cmd_results.pop(cmd_id)
            return jsonify({"status":"done","response":r.get("response",""),"type":r.get("type","")})
    return jsonify({"status":"processing"})


# ── Memory routes ─────────────────────────────────────────────────────────────
@app.route("/api/memory")
def api_memory():
    mems = [m for m in _load(MEMORY_FILE) if m.get("user") == GUEST_USERNAME]
    return jsonify({"memories": list(reversed(mems))})

@app.route("/api/memory/clear", methods=["POST"])
def api_memory_clear():
    _save(MEMORY_FILE, [m for m in _load(MEMORY_FILE) if m.get("user") != GUEST_USERNAME])
    return jsonify({"status":"ok"})

@app.route("/api/memory/add", methods=["POST"])
def api_memory_add():
    data = request.json or {}
    obj  = data.get("object","").strip()
    desc_text = data.get("description","").strip()
    if not obj:
        d = analyzer.latest() if analyzer else None
        if d and d.objects:
            obj = d.objects[0]; desc_text = d.caption
        else:
            return jsonify({"status":"no_object"}), 400
    loc = getattr(d, "location", None) if d else None
    upsert_memory(GUEST_USERNAME, obj, desc_text, loc)
    return jsonify({"status":"saved","object":obj})

@app.route("/api/vision/status")
def api_vision_status():
    return jsonify({"initialized":vision_initialized,"running":vision_running,
                    "error": vision_error.splitlines()[0] if vision_error else None})


# ── Cleanup ───────────────────────────────────────────────────────────────────
def _cleanup():
    log("APP","Shutting down...")
    try:
        if analyzer: analyzer.close()
    except: pass
    try:
        if webcam_stream: webcam_stream.stop()
    except: pass
    voice_cmd_queue.put(None)

atexit.register(_cleanup)

if __name__ == "__main__":
    import sys
    use_https = "--https" in sys.argv

    if use_https:
        # HTTPS mode — required for getUserMedia (phone camera) on mobile browsers.
        # Uses a self-signed cert via pyOpenSSL (pip install pyopenssl).
        # Your phone will show a "not secure" warning — tap Advanced → Proceed.
        # Run with:  python app.py --https
        try:
            app.run(host="0.0.0.0", port=5000, debug=False,
                    use_reloader=False, threaded=True, ssl_context="adhoc")
            log("APP", "Running on https://0.0.0.0:5000 (self-signed cert)")
        except Exception as e:
            log("APP", f"HTTPS failed ({e}) — install pyopenssl: pip install pyopenssl")
            app.run(host="0.0.0.0", port=5000, debug=True,
                    use_reloader=False, threaded=True)
    else:
        # HTTP mode — works on desktop, but phone camera requires HTTPS.
        # Use --https flag to enable camera on mobile.
        app.run(host="0.0.0.0", port=5000, debug=True,
                use_reloader=False, threaded=True)