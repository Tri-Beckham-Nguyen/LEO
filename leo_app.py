"""
LEO's ambient face: one question, one answer on screen at a time.
On launch it greets you with today's agenda. All thinking lives in leo.py.
Run from the LEO folder:  python leo_app.py
"""
import threading
import customtkinter as ctk

from leo import ask_brain, todays_agenda, _mark_hard, _ask_brain_cloud, _console_confirm  # same brain as the terminal

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

GREEN = "#00ff9f"
DIM = "#5f8f79"
FONT = ("Consolas", 13)

conversation = []  # kept in the background so follow-up questions have context


class LeoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LEO")
        self.attributes("-topmost", True)

        w, h = 400, 320
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 64}")  # bottom-right corner
        self.minsize(320, 240)

        self._anim = None

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
        self.entry.bind("<Return>", self._on_return)          # Enter sends
        self.entry.bind("<Shift-Return>", lambda e: None)     # Shift+Enter = newline
        self.send_btn = ctk.CTkButton(row, text="Send", width=64, command=self.on_send)
        self.send_btn.pack(side="left", padx=(8, 0))
        # You are the only reliable judge of a fluent-but-wrong local answer.
        self.fail_btn = ctk.CTkButton(row, text="\u2717 failed", width=70,
                                      fg_color="#8a3b3b", command=self.on_failed)
        self.fail_btn.pack(side="left", padx=(6, 0))
        self.entry.focus()

        # Greet with today's agenda, fetched off the UI thread so the window
        # doesn't freeze while it hits the network.
        self._show("Loading your agenda...")
        threading.Thread(target=self._load_agenda, daemon=True).start()

    def _load_agenda(self):
        try:
            text = todays_agenda()
        except Exception as e:
            text = f"Online. Ask me anything.\n\n(agenda unavailable: {e})"
        self.after(0, lambda: self._show(text))

    def _show(self, text):
        self.response.configure(state="normal")
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

    def _think(self, text):
        checkpoint = len(conversation)
        conversation.append({"role": "user", "content": text})
        try:
            reply = ask_brain(conversation, confirm=self._confirm)
        except Exception as e:
            del conversation[checkpoint:]
            reply = f"[error] {e}"
        self.after(0, lambda: self._finish(reply))

    def on_failed(self):
        """Mark the last question as cloud-only forever, then redo it on cloud."""
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
        self.after(0, lambda: self._finish(reply))

    def _confirm(self, description):
        """Called from the worker thread. Shows a modal approval dialog on the UI
        thread and blocks until Beckham answers. Returns True ONLY on Approve."""
        done = threading.Event()
        result = {"ok": False}
        dlg_ref = {}

        def ask():
            dlg = ctk.CTkToplevel(self)
            dlg.title("Approve this action?")
            dlg.attributes("-topmost", True)
            dlg.lift()
            dlg.focus_force()      # make sure it cannot hide behind a window
            dlg.bell()             # audible: you are being asked for permission
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
            dlg_ref["d"] = dlg
            dlg.protocol("WM_DELETE_WINDOW", lambda: finish(False))  # closing = cancel
            dlg.grab_set()

        self.after(0, ask)
        # Never block forever: an unanswered gate used to hang LEO for good.
        if not done.wait(timeout=120):
            self.after(0, lambda: (dlg_ref.get("d") and dlg_ref["d"].destroy()))
            return False           # timed out = DENIED (safe default)
        return result["ok"]

    def _finish(self, reply):
        self._stop_anim()
        self._show(reply)
        self._set_busy(False)


if __name__ == "__main__":
    LeoApp().mainloop()