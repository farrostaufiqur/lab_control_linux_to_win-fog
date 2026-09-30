"""Full-screen lock overlay (Tkinter). Runs on the main thread."""
import time
import tkinter as tk

BG, CARD, FG, MUTED, ACCENT, DANGER = "#0b1220", "#151d2e", "#f1f5f9", "#94a3b8", "#3b82f6", "#ef4444"
TICK_MS = 200
CONFIRM_SECONDS = 4


def format_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.999))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Overlay:
    def __init__(self, agent, winapi, cfg):
        self.agent, self.winapi, self.cfg = agent, winapi, cfg
        self.blocker = winapi.KeyboardBlocker()
        self.policies = winapi.LockdownPolicies()
        self._confirm: dict[str, float] = {}

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=BG, cursor="arrow")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._build_card()
        self._build_warning()
        self._shown = False
        self._tick()

    # layout --------------------------------------------------------------
    def _build_card(self):
        card = tk.Frame(self.root, bg=CARD, padx=56, pady=44)
        card.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(card, text="🔒", font=("Segoe UI Emoji", 46), bg=CARD, fg=FG).pack()
        self.lab = tk.Label(card, text="", font=("Segoe UI", 12), bg=CARD, fg=MUTED)
        self.lab.pack(pady=(4, 0))
        tk.Label(card, text="Komputer Terkunci", font=("Segoe UI", 28, "bold"), bg=CARD, fg=FG).pack(pady=(4, 10))
        self.message = tk.Label(card, text="", font=("Segoe UI", 13), bg=CARD, fg=FG, wraplength=560, justify="center")
        self.message.pack()
        self.notice = tk.Label(card, text="", font=("Segoe UI", 14, "bold"), bg=ACCENT, fg="#fff",
                               wraplength=560, justify="center", padx=20, pady=12)
        self._notice_text = ""
        self.countdown_caption = tk.Label(card, text="", font=("Segoe UI", 11), bg=CARD, fg=MUTED)
        self.countdown_caption.pack(pady=(22, 0))
        self.countdown = tk.Label(card, text="", font=("Consolas", 40, "bold"), bg=CARD, fg=ACCENT)
        self.countdown.pack()
        buttons = tk.Frame(card, bg=CARD)
        buttons.pack(pady=(28, 0))
        self.buttons = {
            "restart": self._button(buttons, "Restart", "restart"),
            "shutdown": self._button(buttons, "Matikan", "shutdown"),
        }
        self.buttons["restart"].pack(side="left", padx=8)
        self.buttons["shutdown"].pack(side="left", padx=8)
        self.status = tk.Label(card, text="", font=("Segoe UI", 10), bg=CARD, fg=MUTED)
        self.status.pack(pady=(24, 0))

    def _button(self, parent, text, action):
        return tk.Button(
            parent, text=text, font=("Segoe UI", 12, "bold"), width=16, pady=8, bd=0, relief="flat",
            bg=BG, fg=FG, activebackground=ACCENT, activeforeground="#fff", cursor="hand2",
            command=lambda: self._on_button(action),
        )

    def _build_warning(self):
        self.warning = tk.Toplevel(self.root)
        self.warning.withdraw()
        self.warning.overrideredirect(True)
        self.warning.attributes("-topmost", True)
        self.warning.configure(bg=DANGER)
        self.warning_label = tk.Label(self.warning, text="", font=("Segoe UI", 12, "bold"),
                                      bg=DANGER, fg="#fff", padx=18, pady=10)
        self.warning_label.pack()

    # behaviour -----------------------------------------------------------
    def _on_button(self, action):
        now = time.time()
        if now - self._confirm.get(action, 0) <= CONFIRM_SECONDS:
            self._confirm.clear()
            self.agent.user_power(action)
            return
        self._confirm[action] = now

    def _refresh_buttons(self, now):
        labels = {"restart": "Restart", "shutdown": "Matikan"}
        for action, button in self.buttons.items():
            pending = now - self._confirm.get(action, 0) <= CONFIRM_SECONDS
            button.configure(text="Klik lagi untuk konfirmasi" if pending else labels[action],
                             bg=DANGER if pending else BG)

    def _tick(self):
        try:
            self._update()
        except Exception as exc:
            self.agent.add_event("info", f"Kesalahan overlay: {exc!r}")
        self.root.after(TICK_MS, self._tick)

    def _update(self):
        now = time.time()
        view = self.agent.view(now)
        errors = self.policies.apply(view.locked, self.cfg.always_block_taskmgr and not view.exempt)
        if errors:  # kunci layar tetap jalan walau kebijakan registry gagal
            self.agent.add_event("info", "Kebijakan registry gagal ditulis: " + "; ".join(errors))
        self.blocker.active = view.locked

        if view.locked:
            self._show_overlay(view, now)
        else:
            self._hide_overlay()
        self._update_warning(view, now)

    def _show_overlay(self, view, now):
        x, y, w, h = self.winapi.virtual_screen()
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.lab.configure(text=f"{view.lab_name}  ·  {self.agent.host}")
        self.message.configure(text=view.message)
        if view.notice != self._notice_text:
            self._notice_text = view.notice
            if view.notice:
                self.notice.configure(text="Pesan dari admin:\n" + view.notice)
                self.notice.pack(pady=(18, 0), before=self.countdown_caption)
            else:
                self.notice.pack_forget()
        if view.ends_at is not None:
            self.countdown_caption.configure(text="Kunci akan dibuka dalam")
            self.countdown.configure(text=format_countdown(view.ends_at - now))
        else:
            self.countdown_caption.configure(text="")
            self.countdown.configure(text="Menunggu admin", font=("Segoe UI", 24, "bold"))
        self.status.configure(text="Terhubung ke server" if view.connected else "Tidak terhubung ke server")
        self._refresh_buttons(now)
        if not self._shown:
            self.root.deiconify()
            self._shown = True
        self.root.attributes("-topmost", True)
        self.winapi.force_topmost(self.root.winfo_id())

    def _hide_overlay(self):
        if self._shown:
            self.root.withdraw()
            self._shown = False
            self._confirm.clear()
            self.countdown.configure(font=("Consolas", 40, "bold"))

    def _update_warning(self, view, now):
        remaining = None if view.ends_at is None else view.ends_at - now
        if not view.locked and remaining is not None and 0 < remaining <= view.warn_seconds:
            self.warning_label.configure(
                text=f"Sesi lab berakhir dalam {format_countdown(remaining)}. Simpan pekerjaan Anda!")
            self.warning.update_idletasks()
            width, height = self.warning.winfo_reqwidth(), self.warning.winfo_reqheight()
            x, y, w, h = self.winapi.virtual_screen()
            self.warning.geometry(f"+{x + w - width - 24}+{y + h - height - 64}")
            self.warning.deiconify()
            self.warning.attributes("-topmost", True)
        else:
            self.warning.withdraw()

    def run(self):
        self.root.mainloop()
