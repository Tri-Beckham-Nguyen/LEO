"""
LEO's ambient face: one question, one answer on screen at a time.
On launch it greets you with today's agenda. All thinking lives in leo.py.
Run from the LEO folder:  python leo_app.py
"""
import queue
import threading
import time
import customtkinter as ctk

from leo import ask_brain, todays_agenda, _mark_hard, _ask_brain_cloud, load_conversation, save_conversation

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

GREEN = "#00ff9f"
DIM = "#5f8f79"
FONT = ("Consolas", 13)


class LeoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LEO")
        self.attributes("-topmost", True)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 480, 620
        x = sw - w - 20
        y = 40
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(320, 240)

        self._anim = None
        self._ui_q = queue.Queue()
        self._confirm_q = queue.Queue()

        self.conversation = load_conversation()

        self._last_question = ""

        ctk.CTkLabel(self, text="LEO", font=("Consolas", 20, "bold"),
                     text_color=GREEN).pack(pady=(10, 2))

        self.q_label = ctk.CTkLabel(self, text="", font=("Consolas", 11),
                                    text_color=DIM, wraplength=w - 30,
                                    justify="left", anchor="w")
        self.q_label.pack(fill="x", padx=14, pady=(0, 2))

        self.response = ctk.CTkTextbox(self, font=FONT, wrap="word",
                                       fg_color="#000000", text_color="#d7ffe9")
        self.response.pack(fill="both", expand=True, padx=10, pady=4)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 10))
        self.entry = ctk.CTkTextbox(row, font=FONT, wrap="word", height=64,
                                    fg_color="#141a17")
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.send_btn = ctk.CTkButton(row, text="Send", width=64, command=self.on_send)
        self.send_btn.pack(side="left", padx=(8, 0))
        self.fail_btn = ctk.CTkButton(row, text="✕ failed", width=70,
                                      fg_color="#8a3b3b", command=self.on_failed)
        self.fail_btn.pack(side="left", padx=(6, 0))
        self.entry.focus()

        # Additional buttons row
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkButton(tools, text="🎤 Voice", width=70, command=self.voice_input).pack(side="left", padx=2)
        ctk.CTkButton(tools, text="🌓 Theme", width=70, command=self.toggle_theme).pack(side="left", padx=2)
        ctk.CTkButton(tools, text="📜 History", width=70, command=self.show_history).pack(side="left", padx=2)
        ctk.CTkButton(tools, text="🧹 Clear", width=70, command=self.clear_conversation).pack(side="left", padx=2)

        self.after(50, self._poll_ui)
        self.after(100, self._poll_confirms)

        self._show("Loading agenda...")
        threading.Thread(target=self._load_agenda, daemon=True).start()

        # Proactive agenda checker
        self._last_agenda = None
        self.proactive_thread = threading.Thread(target=self._proactive_loop, daemon=True)
        self.proactive_thread.start()

    # ---- cross-thread UI helpers ----------------------------------------

    def _ui_call(self, fn, *args, **kwargs):
        self._ui_q.put((fn, args, kwargs))

    def _poll_ui(self):
        try:
            while True:
                fn, args, kwargs = self._ui_q.get_nowait()
                fn(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(50, self._poll_ui)

    def _poll_confirms(self):
        try:
            while True:
                description, done, result = self._confirm_q.get_nowait()
                if result.get("timed_out"):
                    continue
                self._open_confirm_dialog(description, done, result)
        except queue.Empty:
            pass
        self.after(100, self._poll_confirms)

    # ---- agenda loading --------------------------------------------------

    def _load_agenda(self):
        try:
            text = todays_agenda()
            self._last_agenda = text
        except Exception as e:
            text = f"Online. Ask me anything.\n\n(agenda unavailable: {e})"
        self._ui_call(self._show, text)

    # ---- proactive loop --------------------------------------------------

    def _proactive_loop(self):
        while True:
            time.sleep(3600)  # check every hour
            try:
                agenda = todays_agenda()
                if agenda != self._last_agenda:
                    self._ui_call(self._show, agenda)
                    self._last_agenda = agenda
            except Exception:
                pass

    # ---- display helpers -------------------------------------------------

    def _show(self, text, font=None):
        self.response.configure(state="normal")
        self.response.configure(font=font or FONT)
        self.response.delete("1.0", "end")
        self.response.insert("1.0", text)
        self.response.configure(state="disabled")

    def _animate(self, n=0):
        self._show("thinking" + "." * (1 + n % 3))
        self._anim = self.after(400, self._animate, n + 1)

    def _stop_anim(self):
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None

    def _set_busy(self, busy):
        self.send_btn.configure(state="disabled" if busy else "normal")
        self.entry.configure(state="disabled" if busy else "normal")
        if not busy:
            self.entry.focus()

    # ---- user input ------------------------------------------------------

    def _on_return(self, event):
        self.on_send()
        return "break"

    def on_send(self):
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        self.entry.delete("1.0", "end")
        self._last_question = text
        self.q_label.configure(text=f"> {text}")
        self._set_busy(True)
        self._animate()
        threading.Thread(target=self._think, args=(text,), daemon=True).start()

    def voice_input(self):
        """Capture voice using sounddevice and recognize with Google."""
        try:
            import sounddevice as sd
            import numpy as np
            import speech_recognition as sr

            r = sr.Recognizer()
            self._show("Listening...")
            duration = 5  # seconds
            sample_rate = 16000
            audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
            sd.wait()
            raw = audio.tobytes()
            audio_data = sr.AudioData(raw, sample_rate, 2)
            text = r.recognize_google(audio_data)
            self._ui_call(self.entry.insert, "1.0", text)
        except Exception as e:
            self._ui_call(self._show, f"Voice error: {e}")

    def toggle_theme(self):
        current = ctk.get_appearance_mode()
        new = "Light" if current == "Dark" else "Dark"
        ctk.set_appearance_mode(new)

    def show_history(self):
        """Open a window showing the current conversation history."""
        win = ctk.CTkToplevel(self)
        win.title("Conversation History")
        win.geometry("600x400")
        win.attributes("-topmost", True)
        box = ctk.CTkTextbox(win, font=("Consolas", 12), wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=10)
        text = ""
        for msg in self.conversation:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b.get("text", ""))
                        elif b.get("type") == "tool_use":
                            parts.append(f"[tool: {b.get('name')}]")
                content = " ".join(parts)
            text += f"{role.upper()}: {content}\n\n"
        box.insert("1.0", text)
        box.configure(state="disabled")

    def clear_conversation(self):
        """Reset the conversation."""
        self.conversation = []
        save_conversation(self.conversation)
        self._last_question = ""
        self.q_label.configure(text="")
        self._show("Conversation cleared.")

    def _think(self, text):
        checkpoint = len(self.conversation)
        self.conversation.append({"role": "user", "content": text})
        try:
            reply = ask_brain(self.conversation, confirm=self._confirm)
        except Exception as e:
            del self.conversation[checkpoint:]
            reply = f"[error] {e}"
        self._ui_call(self._finish, reply)

    def on_failed(self):
        q = getattr(self, "_last_question", "")
        if not q:
            return
        _mark_hard(q)
        self._show("Marked as cloud-only. Retrying on the cloud brain...")
        self._set_busy(True)
        threading.Thread(target=self._redo_cloud, args=(q,), daemon=True).start()

    def _redo_cloud(self, q):
        convo = [{"role": "user", "content": q}]
        try:
            reply = _ask_brain_cloud(convo, self._confirm)
        except Exception as e:
            reply = f"[error] {e}"
        self._ui_call(self._finish, reply)

    # ---- approval dialog --------------------------------------------------

    def _confirm(self, description):
        done = threading.Event()
        result = {"ok": False}
        self._confirm_q.put((description, done, result))
        if not done.wait(timeout=120):
            result["timed_out"] = True
            return False
        return result["ok"]

    def _open_confirm_dialog(self, description, done, result):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Approve this action?")
        dlg.attributes("-topmost", True)
        dlg.lift()
        dlg.focus_force()
        dlg.bell()
        dlg.geometry("560x460")
        ctk.CTkLabel(dlg, text="LEO wants to run this. READ it, then decide.",
                     text_color=GREEN, font=("Consolas", 14, "bold")).pack(pady=(10, 4))
        box = ctk.CTkTextbox(dlg, font=("Consolas", 12), wrap="none",
                             fg_color="#0b0f0d", text_color="#d7ffe9")
        box.pack(fill="both", expand=True, padx=10, pady=6)
        box.insert("1.0", description)
        box.configure(state="disabled")
        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(pady=10)

        def finish(ok):
            result["ok"] = ok
            dlg.destroy()
            done.set()

        ctk.CTkButton(btns, text="Approve", fg_color="#00ff9f", text_color="black",
                      command=lambda: finish(True)).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Cancel", fg_color="#d43f3f",
                      command=lambda: finish(False)).pack(side="left", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dlg.grab_set()

    # ---- finish ----------------------------------------------------------

    def _finish(self, reply):
        self._stop_anim()
        self._show(reply)
        self._set_busy(False)
        save_conversation(self.conversation)


if __name__ == "__main__":
    LeoApp().mainloop()