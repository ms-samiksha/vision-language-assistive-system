"""See & Tell — Flask backend. Windows CPU optimized."""
import os, json, re, time, datetime, threading, queue, sys, uuid, atexit
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

import pythoncom   # pip install pywin32

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

# Caption tracking — only speak/show on new events
last_sent_caption     : str   = ""
last_caption_update_t : float = 0.0
caption_lock          = threading.Lock()

# Search mode — pauses captions
searching_mode      : bool = False
searching_mode_lock = threading.Lock()

# ── Logging ───────────────────────────────────────────────────────────────────
def log(cat: str, msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{cat}] {msg}", flush=True)


# ── Windows SAPI speech ───────────────────────────────────────────────────────
speech_queue: queue.Queue = queue.Queue()

def _speech_worker() -> None:
    pythoncom.CoInitialize()
    try:
        import win32com.client
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        speaker.Rate = -1   # slightly slower than default — clearer
        log("SPEECH", "SAPI ready")
    except Exception as e:
        log("SPEECH", f"SAPI unavailable: {e} — browser TTS will be used")
        pythoncom.CoUninitialize()
        return
    while True:
        try:
            text = speech_queue.get(timeout=1.0)
            if text is None:
                break
            speaker.Speak(text)
            speech_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            log("SPEECH_ERR", str(e))
            try: speech_queue.task_done()
            except: pass
    pythoncom.CoUninitialize()

threading.Thread(target=_speech_worker, daemon=True, name="SAPI").start()

def clear_speech_queue() -> None:
    drained = 0
    while True:
        try: speech_queue.get_nowait(); drained += 1
        except queue.Empty: break
    if drained:
        log("SPEECH", f"Cleared {drained} queued items")


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
        if user_data.get("auto_speak", True):
            speech_queue.put(result["response"])
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

def upsert_memory(username: str, obj: str, description: str) -> None:
    memories  = _load(MEMORY_FILE)
    now_str   = datetime.datetime.now().strftime("%I:%M %p")
    obj_lower = obj.lower()
    obj_norm  = _normalize(obj_lower)
    for m in memories:
        if m.get("user") != username: continue
        mn = _normalize(m.get("object","").lower())
        if mn == obj_norm or obj_lower in m.get("object","").lower():
            m["description"] = description
            m["timestamp"]   = now_str
            _save(MEMORY_FILE, memories)
            log("MEMORY", f"Updated '{obj}'")
            return
    memories.append({"object": obj, "description": description,
                     "timestamp": now_str, "user": username})
    _save(MEMORY_FILE, memories)
    log("MEMORY", f"Saved '{obj}'")


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
        clear_speech_queue()
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
        clear_speech_queue()
        return {"response": "Resuming live captions.", "type": "search_end"}

    # START — if vision already initializing/running, don't restart
    START_KWS = ["start captions","start vision","begin captions","begin vision"]
    if cmd.strip() in ("start","begin") or any(kw in cmd for kw in START_KWS):
        if vision_running or vision_initialized:
            return {"response": "Already running.", "type": "already_running"}
        vision_running = True
        threading.Thread(target=_init_vision, daemon=True).start()
        return {"response": "Starting up. Please wait.", "type": "start"}

    # ─── Find / where is ─────────────────────────────────────────────────────
    user     = GUEST_USER
    imp_objs = user.get("important_objects", [])
    common   = ["phone","cell","keys","key","wallet","glasses","laptop","bottle",
                "water bottle","bag","backpack","remote","book","tablet","watch"]
    all_objs = list(imp_objs) + [o for o in common if o not in imp_objs]

    WHERE_KWS = ["where is","where's","where are","where did","where was",
                 "find my","find the","locate my","lost my","i lost"]
    with searching_mode_lock:
        in_search = searching_mode

    if any(kw in cmd for kw in WHERE_KWS) or in_search:
        memories  = _load(MEMORY_FILE)
        user_mems = [m for m in memories if m.get("user") == username]
        found_obj = None
        for obj in all_objs:
            if re.search(r'\b' + re.escape(obj) + r'\b', cmd):
                found_obj = obj; break
        if not found_obj:
            cn = _normalize(cmd)
            for obj in all_objs:
                if _normalize(obj) in cn or cn in _normalize(obj):
                    found_obj = obj; break
        if found_obj:
            nf = _normalize(found_obj)
            for m in reversed(user_mems):
                mn = m.get("object","").lower()
                if found_obj in mn or _normalize(mn) == nf:
                    resp = (f"Your {m['object']} was last seen {m['description']} "
                            f"at {m['timestamp']}.")
                    return {"response": resp, "type": "location"}
            return {"response": f"I haven't seen your {found_obj} recently.",
                    "type": "not_found"}
        if user_mems:
            items = [m.get("object","?") for m in reversed(user_mems[:5])]
            return {"response": f"I can help find: {', '.join(items)}. Which one?",
                    "type": "ask"}
        return {"response": "No items in memory yet.", "type": "empty"}

    # ─── Save ─────────────────────────────────────────────────────────────────
    if any(kw in cmd for kw in ["save","remember","memorize"]):
        desc = analyzer.latest() if analyzer else None
        if desc and desc.objects:
            upsert_memory(username, desc.objects[0], desc.caption)
            return {"response": f"Saved {desc.objects[0]}.", "type": "saved"}
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

            # Start webcam first so frames are ready when analyzer starts
            webcam_stream = WebcamStream(
                index  = vision_config.camera_index,
                width  = vision_config.frame_width,
                height = vision_config.frame_height,
            )
            webcam_stream.start()
            log("VISION", f"Webcam {vision_config.frame_width}x{vision_config.frame_height} open")

            # Create analyzer and inject webcam so it feeds itself continuously
            analyzer = FrameAnalyzer(vision_config, model, webcam=webcam_stream)

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
                result = (f"Your {m['object']} was last seen {m['description']} "
                          f"at {m['timestamp']}.")
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
    import cv2 as _cv2
    def generate():
        while True:
            if not vision_running or webcam_stream is None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _PLACEHOLDER_JPEG + b"\r\n"
                time.sleep(0.25)
                continue
            frame = webcam_stream.read()
            if frame is None:
                time.sleep(0.05)
                continue
            ok, buf = _cv2.imencode(".jpg", frame, [_cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            time.sleep(0.05)   # ~20 FPS

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

    with caption_lock:
        should_announce = (
            new_event is not None
            and new_event.caption != last_sent_caption
            and "Initializing" not in new_event.caption
        )
        if should_announce:
            last_sent_caption     = new_event.caption
            last_caption_update_t = now
            log("CAPTION", f"ANNOUNCE: {new_event.caption}")
            if auto_speak:
                # Do NOT clear_speech_queue here — that was cutting off speech mid-sentence.
                # Instead just enqueue. SAPI will play them sequentially.
                # Only clear if the queue is backing up (>1 item waiting).
                qsize = speech_queue.qsize()
                if qsize > 1:
                    clear_speech_queue()
                speech_queue.put(new_event.caption)
            _auto_save(new_event)
            desc = new_event  # use new_event for the response below

    return jsonify({
        "status":     status,
        "caption":    last_sent_caption or desc.caption,
        "objects":    desc.objects,
        "actions":    desc.actions,
        "updated_at": _ts(),
        "is_new":     should_announce if 'should_announce' in dir() else False,
    })

def _ts():
    return datetime.datetime.now().strftime("%I:%M %p")

def _auto_save(desc: FrameDescription) -> None:
    """
    Save important objects to memory — called once per new event.
    Saves ALL objects in the frame that match the important list,
    so if someone is holding a phone AND wearing glasses, both get saved.
    Caption context is stored so memory is rich ("holding a phone while sitting").
    """
    important = GUEST_USER.get("important_objects", [])
    if not desc.objects:
        return
    saved = set()
    for obj in desc.objects:
        obj_lower = obj.lower()
        # Check against important list first
        for imp in important:
            if imp in obj_lower or obj_lower in imp:
                if obj_lower not in saved:
                    saved.add(obj_lower)
                    threading.Thread(
                        target=upsert_memory,
                        args=(GUEST_USERNAME, obj, desc.caption),
                        daemon=True,
                    ).start()
                break
        # Also always save phone/cell/keys even if not in important list
        # — these are too useful to miss
        if any(kw in obj_lower for kw in ("phone","cell","key","wallet")) and obj_lower not in saved:
            saved.add(obj_lower)
            threading.Thread(
                target=upsert_memory,
                args=(GUEST_USERNAME, obj, desc.caption),
                daemon=True,
            ).start()


# ── Voice command routes ──────────────────────────────────────────────────────
def _queue_command(command: str) -> str:
    clear_speech_queue()
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
    upsert_memory(GUEST_USERNAME, obj, desc_text)
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
    speech_queue.put(None)
    voice_cmd_queue.put(None)

atexit.register(_cleanup)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False, threaded=True)