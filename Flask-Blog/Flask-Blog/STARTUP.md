# See & Tell - Flask UI with Live Vision Integration

This Flask application now has real-time vision model integration. The camera feed is processed by the BLIP vision model in the background, and captions appear in the web UI without opening a separate camera window.

## 🚀 How to Start

### 1. **Activate Virtual Environment** (from project root)
```bash
# Windows
.venv\Scripts\Activate.ps1

# Linux/Mac
source .venv/bin/activate
```

### 2. **Navigate to Flask App**
```bash
cd Flask-Blog/Flask-Blog
```

### 3. **Run the Flask App**
```bash
python app.py
```

**What happens:**
- Flask starts on `http://localhost:5000`
- Vision pipeline initializes automatically
- Camera starts capturing frames
- Model loads and begins analyzing frames
- Each frame is processed continuously in a background thread

### 4. **Open in Browser**
- Go to `http://localhost:5000`
- Sign up or login
- Navigate to **Dashboard**
- You'll see **live captions** from your camera appearing automatically

---

## 📊 How It Works

### Architecture
```
Webcam → FrameAnalyzer → Vision Model (BLIP) 
                ↓
         Real-time captions
                ↓
      /api/caption endpoint
                ↓
         Dashboard Web UI
                ↓
    Display + Optional Text-to-Speech
```

### No Separate Window!
- ❌ Old way: `main.py` opens an OpenCV window with camera stream
- ✅ New way: Flask app processes camera in background, all interaction via web UI

### Data Flow
1. **Frame Capture**: `WebcamStream` continuously reads from your webcam
2. **Background Processing**: Thread feeds frames to `FrameAnalyzer`  
3. **Vision Model**: BLIP model generates captions for each frame
4. **Smoothing**: Captions are stabilized via majority voting
5. **Extraction**: Objects and actions are extracted from captions
6. **API Response**: Latest caption served via `/api/caption`
7. **Web UI**: JavaScript polls the endpoint every 2 seconds (already configured)
8. **Auto-speak**: If enabled in settings, captions are read aloud

---

## ⚙️ Configuration

### Camera Settings
Set environment variables before starting:
```bash
# Change camera index (0 = default)
$env:APP_CAMERA_INDEX = "0"

# Change frame resolution
$env:APP_FRAME_WIDTH = "1280"
$env:APP_FRAME_HEIGHT = "720"

# Change model
$env:APP_MODEL_NAME = "Salesforce/blip-image-captioning-base"

# Use CPU or GPU
$env:APP_DEVICE = "cpu"  # or "cuda" or "auto"

# Then run app.py
```

---

## 🔍 Monitoring & Debugging

### Check Vision Pipeline Status
The `/api/health` endpoint shows if vision is ready:
```bash
curl http://localhost:5000/api/health
```

### Check Caption Endpoint
```bash
curl http://localhost:5000/api/caption
```

Response:
```json
{
  "status": "LIVE",
  "caption": "A person holding a keyboard on a desk",
  "objects": ["person", "keyboard", "desk"],
  "actions": ["holding"],
  "updated_at": "10:30 AM"
}
```

### Common Issues

**"Vision system error: Unable to open webcam"**
- Your camera isn't detected or already in use
- Try another camera index: `$env:APP_CAMERA_INDEX = "1"`

**Model still loading...**
- First run downloads ~370MB model (takes 1-2 min)
- Wait for "Vision pipeline ready!" message in console

**Slow captions**
- Using CPU? Switch to GPU: `$env:APP_DEVICE = "cuda"`
- Or reduce frame width/height for faster processing

**Camera not showing up**
- Web UI has no video window by design ✓
- Captions will appear in the dashboard
- You should see frames being processed in console logs

---

## 📝 Modified Files

### Core Changes
- **`app.py`** - Main Flask app with vision integration
  - Added imports for camera, vision model, analyzer
  - Initialize vision pipeline on startup
  - Background frame processing thread  
  - Updated `/api/caption` endpoint to return real data
  - Added `/api/health` endpoint for status checks

### Files NOT Changed
- All UI templates remain the same (index.html, dashboard.html, etc.)
- All JavaScript remains the same (app.js)
- All other backend routes unchanged
- Vision model files unchanged (camera/, vision/, utils/)

---

## 🎯 Web UI Features (Already Working)

✅ **Dashboard**
- Live caption display
- Object & action tags
- "Read Aloud" button (text-to-speech)
- "Refresh Caption" button (manual update)
- "Add to Memory" button (save interesting moments)

✅ **Memory System**
- Auto-save captions to user's memory
- Search for previously seen objects
- Track timestamps

✅ **Accessibility Settings**
- Fully Blind mode (high contrast, larger text)
- Low Vision mode (optimized colors)
- Speech rate adjustment
- Important objects alerts

---

## 🛑 Stopping the App

Press `Ctrl+C` in the terminal to shutdown:
- Flask server stops
- Vision pipeline cleanup (camera releases)
- All threads terminate gracefully

---

## 📦 Dependencies

Already in `requirements.txt`:
- Flask, transformers, torch, PIL, opencv-python, etc.

Make sure all are installed:
```bash
pip install -r requirements.txt
```

---

**That's it! Your vision model is now connected to the web UI.** 🎉
