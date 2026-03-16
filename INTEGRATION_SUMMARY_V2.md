# See & Tell — What Changed

## Overview
This document covers all changes made after the initial Flask–BLIP integration (documented in `INTEGRATION_SUMMARY.md`).

---

## Files Modified

### `Flask-Blog/Flask-Blog/app.py`

| What | Why |
|------|-----|
| Removed Windows SAPI / pywin32 entirely | Replaced with browser TTS — works on mobile |
| Added `MobileStream` class | Receives JPEG frames from phone camera via HTTP POST |
| Added `POST /api/camera/frame` endpoint | Phone browser posts frames here every 200ms |
| Added `--https` flag to `app.run()` | Required for camera + mic permissions on mobile Chrome/Safari |
| `upsert_memory()` now appends instead of overwrites | All sightings kept — previously only the latest survived |
| Added `memory_file_lock` (threading.Lock) | Fixed race condition where multiple objects saved simultaneously lost all but one |
| `_auto_save()` saves objects sequentially in one thread | Same race condition fix |
| `_init_vision()` uses `MobileStream` instead of `WebcamStream` | Phone camera replaces PC webcam |
| `/api/caption` response includes `speak_text` and `location` fields | Browser uses these directly |
| Start command checks only `vision_running`, not `vision_initialized` | Model stays loaded — start after stop works correctly |
| Server stays in `searching_mode` until explicit "done searching" | Search result stays on screen |
| Search matches longest object first, last spoken word preferred | Fixes Chrome transcript bleed (e.g. "cell bottle" when user said "bottle") |
| Added `QUERY_ALIASES` dict | Normalises Chrome mishearings: "cell phone"→"cell", "spectacles"→"glasses" etc. |

---

### `static/app.js`

| What | Why |
|------|-----|
| `fetchCaption()` sets `d.caption = d.speak_text` immediately | Caption was being spoken but not displayed on mobile |
| `speakText()` fires synchronously when `delayMs=0` | Removed unnecessary async overhead |
| Mic mute timing recalibrated for browser TTS (~140 wpm) | Was calibrated for SAPI (~75 wpm) — mic was unblocking too early |
| `VOICE_MODE` stays `true` after search result | Search overlay stayed visible until "done searching" |
| In search mode, raw transcript sent before CMD_TABLE lookup | Prevents old spoken words bleeding into new queries |
| `startPolling()` called on `search_end` | Captions resume correctly after searching |
| `visionWasActiveBeforeSearch` flag | Restores correct state after search — stopped stays stopped, running resumes |
| Phone camera: `_startPhoneCamera()`, `_postFrame()`, `_stopPhoneCamera()` | `getUserMedia` capture loop posting frames to server |

---

### `vision/model.py`

| What | Why |
|------|-----|
| Added `HallucinationFilter` class | BLIP was generating violent/impossible captions from news training data |
| `ALWAYS_BLOCK` list (~50 words) | shot, killed, murdered, camel, blood, bomb etc. — never valid in room scenes |
| `CONTEXT_REQUIRED` dict | bathroom/kitchen/bedroom only allowed if supporting words also appear |
| Removed `"a photo of"` prompt | This prompt activates BLIP's photojournalism training data — main cause of violent captions |
| Reverted dual-pass → single-pass | Dual-pass doubled hallucination surface with no benefit |
| Falls back to last clean caption when blocked | Silent fallback instead of speaking garbage |

---

### `vision/inference.py`

| What | Why |
|------|-----|
| Added `LocationExtractor` class | Extracts where objects are: "on the table", "in the kitchen" etc. |
| `FrameDescription` has new `location` field | Carries location through the pipeline to memory and search |
| BLIP loop sleep reduced 5s → 2s | Captions every ~4–5s instead of ~7–8s |
| `"Someoneis"` → `"Someone is"` fixed anywhere in string | Was only caught at start of string |
| Laptop/computer redundancy cleaned up | "using laptop to work on the computer" → "using their laptop" |

---

### `templates/base.html`

| What | Why |
|------|-----|
| Nav height 60px → 48px, logo and links smaller | Nav was too large on mobile — cut off content |
| Voice bar 56px → 48px, transcript span hidden | More vertical space for camera feed |
| All sizes respect `env(safe-area-inset-*)` | Correct layout on notched phones |

---

### `templates/dashboard.html`

| What | Why |
|------|-----|
| Control strip: `flex-wrap` → `overflow-x:auto` | Buttons were wrapping to 2–3 rows on mobile |
| Caption bar padding reduced, min-height 90px → 66px | More space for camera feed |
| `<video>` and `<canvas>` elements added | Phone camera preview and frame capture |
| Search overlay updated with animated indicator and result card | Visual feedback during search |
| Location tag shown in amber alongside object tags | Shows extracted location in caption bar |

---

### `templates/memory.html`

| What | Why |
|------|-----|
| Added Location column | Memory now stores and shows where each object was seen |
| Shows all sightings (not just latest per object) | Previously only most recent entry survived |

---

## Mobile Integration — Step by Step

### Requirements
- PC and phone on the **same Wi-Fi network**
- `pyopenssl` installed: `pip install pyopenssl`

### Step 1 — Find your PC's local IP
Open Command Prompt:
```
ipconfig
```
Look for **IPv4 Address** under Wi-Fi adapter — e.g. `192.168.0.101`

> This IP can change when your PC reconnects to Wi-Fi. Run `ipconfig` again if the app stops working.

### Step 2 — Allow port 5000 through Windows Firewall
Run in **admin PowerShell** (one time):
```powershell
New-NetFirewallRule -DisplayName "Flask See&Tell" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Any
```
> `-Profile Any` is required. Without it the rule is ignored when Windows classifies your Wi-Fi as Public.

### Step 3 — Start the app with HTTPS
```
python app.py --https
```
You should see:
```
* Running on https://0.0.0.0:5000
* Running on https://192.168.0.101:5000
```
> BLIP loads on first start (~30–60 seconds). Wait for `[VISION] Pipeline ready!` before opening on your phone.

### Step 4 — Open on your phone
Make sure your phone is on the **same Wi-Fi** as your PC.  
Open **Chrome** (Android) or **Safari** (iPhone):
```
https://192.168.0.101:5000
```
Replace the IP with yours from Step 1.

### Step 5 — Accept the certificate warning
The browser will show **"Your connection is not private"** — this is normal for a local self-signed certificate.

- **Chrome:** tap **Advanced** → **Proceed to 192.168.0.101 (unsafe)**
- **Safari:** tap **Show Details** → **visit this website** → **Visit Website**

You only need to do this once.

### Step 6 — Allow camera and microphone
Tap **▶ Start**. The browser will ask for two permissions — allow both:
- **Camera** — for the phone camera feed sent to BLIP
- **Microphone** — for voice commands

> If the permissions panel only shows "Sound" and "Desktop site" with no Camera or Microphone options, you are on `http://` not `https://`. Make sure the URL starts with `https://`.

### Step 7 — Add to Home Screen (optional)
Makes the app open full-screen like a native app.

**Android Chrome:** 3-dot menu → **Add to Home screen**  
**iPhone Safari:** Share → **Add to Home Screen**

---

### How it works
The phone camera captures frames and sends them to the PC every 200ms. BLIP runs on the PC and sends captions back to the phone. The phone handles display, text-to-speech, and voice commands. No AI runs on the phone.

---

### Troubleshooting

| Problem | Fix |
|---------|-----|
| `ERR_ADDRESS_UNREACHABLE` | Run `ipconfig` — IP may have changed. Check phone is on same Wi-Fi. |
| `ERR_TIMED_OUT` on phone | BLIP still loading — wait 60s and reload |
| `ERR_TIMED_OUT` on PC too | Re-run firewall rule with `-Profile Any` |
| Camera/mic permissions not shown | Using `http://` not `https://` |
| Works at home, not at college/hostel | Router has AP Isolation — connect PC to phone's mobile hotspot instead |

---

## What Was NOT Changed

- `camera/webcam.py` — unchanged (still used in desktop mode)
- `utils/config.py` — unchanged
- `templates/find.html` — unchanged (minor style only)
- `templates/settings.html` — unchanged (minor style only)
- `requirements.txt` — add `pyopenssl` manually

---

## Project Flow

```
Phone Camera (getUserMedia)
        │
        │  POST /api/camera/frame  (JPEG, every 200ms)
        ▼
MobileStream.push_jpeg()
        │
        │  cv2.imdecode → BGR numpy frame
        ▼
FrameAnalyzer._loop()
        │
        ├── HallucinationFilter   → blocks violent/impossible captions
        ├── AccessibilityFormatter → cleans BLIP output
        ├── ObjectActionExtractor  → detects objects & actions
        ├── LocationExtractor      → extracts "on the table", "in the kitchen"
        └── SceneChangeDetector    → only fires on genuine scene changes
        │
        ▼
FrameDescription(caption, objects, actions, location, timestamp)
        │
        ├── upsert_memory()  → appends to memory.json (with file lock)
        │
        ▼
/api/caption  (polled every 1s by phone browser)
        │
        │  { status, caption, speak_text, objects, location, updated_at }
        ▼
Phone Browser
        ├── Display caption text + location tag + object tags
        ├── window.speechSynthesis.speak(speak_text)
        └── SpeechRecognition → voice commands → /api/voice/command
                                                        │
                                              _process_command()
                                                        │
                                    ┌───────────────────┼───────────────────┐
                                 search             find object           save
                                    │                   │                   │
                              searching_mode=True   memory.json        upsert_memory()
                              overlay shown         lookup + speak
```

---

## Configuration Reference

Set environment variables before starting `app.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_CAMERA_INDEX` | `0` | Camera device index (desktop mode only) |
| `APP_FRAME_WIDTH` | `640` | Capture resolution width |
| `APP_FRAME_HEIGHT` | `480` | Capture resolution height |
| `APP_MODEL_NAME` | `Salesforce/blip-image-captioning-base` | BLIP model to load |
| `APP_DEVICE` | `cpu` | `cpu`, `cuda`, or `auto` |
| `APP_MAX_CAPTION_TOKENS` | `40` | Max tokens BLIP generates per caption |
| `APP_SAMPLE_INTERVAL` | `4000` | ms between BLIP inferences (config only — loop uses 2000ms) |
| `APP_INFERENCE_SHORT_SIDE` | `256` | Downscale short side before feeding BLIP |

**Example (Windows PowerShell):**
```powershell
$env:APP_DEVICE = "cuda"
$env:APP_MODEL_NAME = "Salesforce/blip-image-captioning-large"
python app.py --https
```

---

## Quick Start Cheat Sheet

```
── MOBILE (phone camera) ──────────────────────────────────────

1. pip install pyopenssl

2. ipconfig                        → note IPv4 (e.g. 192.168.0.101)

3. Admin PowerShell (one time):
   New-NetFirewallRule -DisplayName "Flask See&Tell"
     -Direction Inbound -Protocol TCP
     -LocalPort 5000 -Action Allow -Profile Any

4. python app.py --https           → wait for "Pipeline ready!"

5. Phone (same Wi-Fi):
   https://192.168.0.101:5000
   → Accept cert warning → Advanced → Proceed
   → Tap Start → Allow camera → Allow microphone


── DESKTOP (PC webcam) ────────────────────────────────────────

1. python app.py

2. http://localhost:5000 → Tap Start


── IF IP CHANGES ──────────────────────────────────────────────

   ipconfig → get new IP → update phone URL
   (set static IP in router DHCP to avoid this)
```