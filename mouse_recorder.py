
import json
import time
import threading
import queue
import os
import ctypes

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pynput import mouse, keyboard

MOVE_THROTTLE = 0.01  # мин. интервал между записанными событиями движения (сек)

# ---------------- Windows SendInput API ----------------

user32 = ctypes.windll.user32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

INPUT_MOUSE = 0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]


def _send_input(dx=0, dy=0, mouse_data=0, flags=0):
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, mouse_data, flags, 0, None))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _to_absolute(x, y):
    """Нормализует пиксельные координаты в диапазон 0..65535,
    как того требует флаг MOUSEEVENTF_ABSOLUTE."""
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    abs_x = int(x * 65535 / max(screen_w - 1, 1))
    abs_y = int(y * 65535 / max(screen_h - 1, 1))
    return abs_x, abs_y


def raw_move(x, y):
    abs_x, abs_y = _to_absolute(x, y)
    _send_input(abs_x, abs_y, flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)


def raw_click(x, y, button, pressed):
    raw_move(x, y)
    if button == "Button.left":
        flag = MOUSEEVENTF_LEFTDOWN if pressed else MOUSEEVENTF_LEFTUP
    elif button == "Button.right":
        flag = MOUSEEVENTF_RIGHTDOWN if pressed else MOUSEEVENTF_RIGHTUP
    elif button == "Button.middle":
        flag = MOUSEEVENTF_MIDDLEDOWN if pressed else MOUSEEVENTF_MIDDLEUP
    else:
        return
    _send_input(flags=flag)


def raw_scroll(x, y, dx, dy):
    raw_move(x, y)
    _send_input(mouse_data=int(dy * 120), flags=MOUSEEVENTF_WHEEL)


class MouseRecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mouse Recorder")
        self.root.geometry("480x420")
        self.root.resizable(False, False)

        self.events = []
        self.recording = False
        self.playing = False
        self.loop_playback = tk.BooleanVar(value=False)
        self._loop_enabled = False  # обычный bool — безопасно читать из worker-потока
        self.start_time = 0.0
        self.last_move_time = 0.0
        self.current_file = None

        self.log_queue = queue.Queue()

        self._build_ui()
        self._start_listeners()
        self._poll_queue()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI ----------------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        self.status_var = tk.StringVar(value="Готово")
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        ttk.Label(status_frame, text="Статус:", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 11))
        self.status_label.pack(side="left", padx=6)

        self.count_var = tk.StringVar(value="Событий записано: 0")
        ttk.Label(self.root, textvariable=self.count_var).pack(anchor="w", padx=10)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)

        self.record_btn = ttk.Button(btn_frame, text="● Запись (F8)", command=self.toggle_recording)
        self.record_btn.pack(side="left", expand=True, fill="x", padx=4)

        self.play_btn = ttk.Button(btn_frame, text="▶ Воспроизвести (F10)", command=self.toggle_playback)
        self.play_btn.pack(side="left", expand=True, fill="x", padx=4)

        loop_frame = ttk.Frame(self.root)
        loop_frame.pack(fill="x", **pad)
        ttk.Checkbutton(
            loop_frame,
            text="Зациклить воспроизведение",
            variable=self.loop_playback,
            command=self._on_loop_toggle,
        ).pack(anchor="w")

        file_frame = ttk.Frame(self.root)
        file_frame.pack(fill="x", **pad)
        ttk.Button(file_frame, text="Сохранить как...", command=self.save_as).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Button(file_frame, text="Открыть макрос...", command=self.open_file).pack(side="left", expand=True, fill="x", padx=4)

        self.file_var = tk.StringVar(value="Файл: (не сохранён)")
        ttk.Label(self.root, textvariable=self.file_var).pack(anchor="w", padx=10)

        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(self.root, text="Лог:").pack(anchor="w", padx=10)
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(
            self.root,
            text="Esc — выход. Горячие клавиши работают даже вне окна программы.",
            foreground="gray"
        ).pack(anchor="w", padx=10, pady=(0, 6))

    def log(self, message):
        self.log_queue.put(message)

    def _poll_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # ---------------- Запись ----------------

    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if self.playing:
            self.log("Нельзя начать запись во время воспроизведения.")
            return
        self.events = []
        self.start_time = time.time()
        self.recording = True
        self.status_var.set("Запись...")
        self.record_btn.configure(text="■ Остановить запись (F8)")
        self.log("Запись начата.")

    def stop_recording(self):
        self.recording = False
        self.status_var.set("Готово")
        self.record_btn.configure(text="● Запись (F8)")
        self.count_var.set(f"Событий записано: {len(self.events)}")
        self.log(f"Запись остановлена. Событий: {len(self.events)}")

    def on_move(self, x, y):
        if not self.recording:
            return
        now = time.time()
        if now - self.last_move_time < MOVE_THROTTLE:
            return
        self.last_move_time = now
        self.events.append({"type": "move", "x": x, "y": y, "t": now - self.start_time})

    def on_click(self, x, y, button, pressed):
        if not self.recording:
            return
        self.events.append({
            "type": "click", "x": x, "y": y,
            "button": str(button), "pressed": pressed,
            "t": time.time() - self.start_time
        })

    def on_scroll(self, x, y, dx, dy):
        if not self.recording:
            return
        self.events.append({
            "type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy,
            "t": time.time() - self.start_time
        })

    # ---------------- Сохранение / загрузка ----------------

    def save_as(self):
        if not self.events:
            messagebox.showinfo("Нет данных", "Сначала запишите макрос (F8).")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON файлы", "*.json")],
            initialfile="macro.json"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.events, f, ensure_ascii=False, indent=2)
        self.current_file = path
        self.file_var.set(f"Файл: {path}")
        self.log(f"Сохранено: {path}")

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("JSON файлы", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.events = json.load(f)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить файл:\n{e}")
            return
        self.current_file = path
        self.file_var.set(f"Файл: {path}")
        self.count_var.set(f"Событий записано: {len(self.events)}")
        self.log(f"Загружено: {path} ({len(self.events)} событий)")

    # ---------------- Воспроизведение ----------------

    def _on_loop_toggle(self):
        # Обновляем обычный bool в главном потоке при каждом клике по галочке
        self._loop_enabled = bool(self.loop_playback.get())

    def toggle_playback(self):
        if self.playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if self.recording:
            self.log("Нельзя воспроизводить во время записи.")
            return
        if not self.events:
            messagebox.showinfo("Нет данных", "Сначала запишите или загрузите макрос.")
            return
        self.playing = True
        # Синхронизируем флаг в главном потоке перед стартом worker'а
        self._loop_enabled = bool(self.loop_playback.get())
        self.status_var.set("Воспроизведение...")
        self.play_btn.configure(text="■ Остановить (F10)")
        t = threading.Thread(target=self._playback_worker, daemon=True)
        t.start()

    def stop_playback(self):
        self.playing = False

    def _playback_finished_ui(self):
        self.status_var.set("Готово")
        self.play_btn.configure(text="▶ Воспроизвести (F10)")

    def _playback_worker(self):
        self.log(f"Воспроизведение начато. Событий: {len(self.events)}")
        first_run = True
        # Читаем только обычный bool — без обращения к Tk из worker-потока
        while self.playing and (first_run or self._loop_enabled):
            first_run = False
            playback_start = time.time()
            for event in self.events:
                if not self.playing:
                    break
                target_time = playback_start + event["t"]
                delay = target_time - time.time()
                if delay > 0:
                    time.sleep(delay)

                etype = event["type"]
                if etype == "move":
                    raw_move(event["x"], event["y"])
                elif etype == "click":
                    raw_click(event["x"], event["y"], event["button"], event["pressed"])
                elif etype == "scroll":
                    raw_scroll(event["x"], event["y"], event["dx"], event["dy"])

        self.playing = False
        self.log("Воспроизведение завершено.")
        self.root.after(0, self._playback_finished_ui)

    # ---------------- Глобальные горячие клавиши ----------------

    def _start_listeners(self):
        self.mouse_listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll
        )
        self.keyboard_listener = keyboard.Listener(on_press=self._on_key_press)
        self.mouse_listener.start()
        self.keyboard_listener.start()

    def _on_key_press(self, key):
        if key == keyboard.Key.f8:
            self.root.after(0, self.toggle_recording)
        elif key == keyboard.Key.f10:
            self.root.after(0, self.toggle_playback)
        elif key == keyboard.Key.esc:
            self.root.after(0, self.on_close)

    def on_close(self):
        self.recording = False
        self.playing = False
        self.root.destroy()
        os._exit(0)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    app = MouseRecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()