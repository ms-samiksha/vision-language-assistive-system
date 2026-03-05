# See & Tell

A real-time vision–language assistive system for visually impaired users.

## Features
- **Live Captions**: Real-time descriptions of the scene (simulated).
- **Object Memory**: Remembers objects and allows querying their location.
- **Accessibility Modes**:
  - **Standard**: Normal UI.
  - **Low Vision**: High contrast, large text.
  - **Fully Blind**: Voice-first interface with auto-speak.
- **Audio Feedback**: Uses browser SpeechSynthesis for reading captions and results.

## How to Run on Replit
1. Ensure Python 3.11 is installed (handled automatically).
2. Install dependencies: `flask`, `werkzeug` (handled automatically).
3. Run `python3 app.py`.
   - If using Replit's Run button, ensure `.replit` or `package.json` is configured to run `python3 app.py`.

## Login & Accessibility
- **Sign Up**: Create an account and select your vision assistance level.
- **Login**: Access your personalized dashboard.
- **Modes**:
  - **Fully Blind**: Auto-speak is ON by default.
  - **Low Vision**: UI switches to high contrast colors.

## Future Enhancements
- Integration with real Computer Vision API (OpenAI Vision, etc.).
- Hardware integration (camera glasses).
- Voice commands for navigation.
