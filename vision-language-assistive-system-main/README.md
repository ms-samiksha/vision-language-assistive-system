### A Real-Time Vision–Language Assistive System for Visually Impaired Users

---

## Overview

**See and Tell** is a real-time, offline assistive system designed to help visually impaired users understand their surroundings through **spoken natural language descriptions**.

The system captures live video from a camera, analyzes the scene using a **vision–language model**, and converts visual understanding into **audio feedback**.  
It focuses on **context awareness**, not just object detection, and aims to improve independence in everyday indoor environments.

---

## Motivation

Visually impaired individuals often face challenges such as:
- Identifying objects around them
- Understanding nearby activities
- Finding misplaced personal items
- Navigating indoor spaces safely

Many existing solutions are:
- Expensive or uncomfortable (wearables)
- Limited to object labels only
- Cloud-dependent, causing latency and privacy concerns

This project explores a **local-first, privacy-preserving assistive approach** using Vision–Language AI.

---

## Key Features

- 🎥 Live webcam capture  
- 🧠 Vision–Language scene understanding  
- 🗣️ Offline audio narration  
- 🔄 Asynchronous processing for smooth performance  
- 🧩 Caption stabilization to reduce noise  
- 🧠 Short-term object memory for misplaced items  
- 🔐 Fully offline and privacy-friendly  

---

## How the System Works

1. The camera continuously captures video frames.
2. Frames are processed asynchronously by a vision–language model.
3. The model generates a natural language description of the scene.
4. Descriptions are stabilized to avoid flickering output.
5. The final caption is converted into speech and played to the user.
6. Detected objects are temporarily remembered for later reference.

All components operate **without internet connectivity**.

---

## Technologies Used

- **Python** – core programming language  
- **OpenCV** – real-time webcam capture and frame handling  
- **BLIP (Bootstrapped Language–Image Pretraining)** – vision–language model for image captioning  
- **PyTorch** – deep learning inference framework  
- **Windows Speech API (SAPI)** – offline text-to-speech narration  
- **Multithreading and queues** – real-time system stability  

---

## Project Structure

```

main.py
camera/
webcam.py
vision/
model.py
inference.py
utils/
config.py
fps.py
tts.py
requirements.txt
README.md
LICENSE

````

---

## Installation

```bash
python -m venv .venv
. .venv/Scripts/activate   # Windows PowerShell
pip install --upgrade pip
pip install -r requirements.txt
````

> The vision–language model is downloaded automatically on first run and cached locally.

---

## Running the Application

```bash
python main.py
```

* The camera feed starts automatically
* Audio narration begins once the vision system is active
* Press `q` or `ESC` to exit

---

## Configuration

The system can be customized using environment variables (defaults in `utils/config.py`):

| Variable              | Description                      |
| --------------------- | -------------------------------- |
| `APP_CAMERA_INDEX`    | Camera device index              |
| `APP_SAMPLE_INTERVAL` | Time gap between inferences      |
| `APP_MODEL_NAME`      | Vision–Language model identifier |
| `APP_DEVICE`          | CPU / GPU selection              |

---

## Limitations

* Caption accuracy depends on lighting and camera viewpoint
* Vision–Language models may generate generic descriptions in complex scenes
* Object memory is short-term by design to preserve privacy

---

## Future Enhancements

* Voice-based user interaction
* Multilingual audio output
* Mobile or wearable deployment
* Confidence-aware narration
* Improved object tracking

---

## License

This project is licensed under the **MIT License**.

It uses a pre-trained Vision–Language model (BLIP) provided by its original authors under their respective license.
Model weights are **not distributed** with this repository.
