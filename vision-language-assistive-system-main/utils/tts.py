import queue
import win32com.client


class TextToSpeech:
    def __init__(self):
        # Native Windows Speech API
        self.voice = win32com.client.Dispatch("SAPI.SpVoice")
        self.queue = queue.Queue()

    def enqueue(self, text: str):
        if text:
            self.queue.put(text)

    def process(self):
        """
        Must be called from main thread.
        Speaks exactly one item per call.
        """
        if not self.queue.empty():
            text = self.queue.get()
            self.voice.Speak(text)
            self.queue.task_done()
