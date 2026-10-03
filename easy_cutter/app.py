import bisect
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .video import EPS, VideoReader, export_clip, frame_times

DEFAULT_LENGTH = 5.0
# Kleiner als ein Frame, damit sich der Bereich per Frame-Buttons bis auf einen Frame verkürzen lässt
MIN_LENGTH = 0.01
# Vorschaubilder werden höchstens in dieser Größe dekodiert und dann auf die Fenstergröße skaliert
PREVIEW_SIZE = (1280, 720)
POLL_MS = 20

COLORS = {
    'bg': '#16181d',
    'panel': '#1f232b',
    'border': '#323844',
    'text': '#e6e8ec',
    'muted': '#8a93a3',
    'accent': '#3d8bfd',
    'selection': '#2a4f86',
    'playhead': '#ff5a5a',
}


def format_time(seconds):
    m, s = divmod(max(seconds, 0.0), 60)
    return f'{int(m)}:{s:06.3f}'


class FrameWorker:
    """Dekodiert Standbilder im Hintergrund. Nur die jeweils letzte Anfrage wird bearbeitet."""

    def __init__(self, name, mode, results):
        self.name = name
        self.mode = mode  # 'at': erster Frame >= t, 'before': letzter Frame < t
        self.results = results
        self._cond = threading.Condition()
        self._request = None
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, path, t):
        with self._cond:
            self._request = (path, t)
            self._cond.notify()

    def _run(self):
        reader = None
        while True:
            with self._cond:
                while self._request is None:
                    self._cond.wait()
                path, t = self._request
                self._request = None
            try:
                if reader is None or reader.path != path:
                    if reader:
                        reader.close()
                    reader = VideoReader(path)
                if self.mode == 'at':
                    av_frame = reader.first_frame_at(t)
                else:
                    av_frame = reader.last_frame_before(t)
                frame = reader.to_frame(av_frame, PREVIEW_SIZE) if av_frame else None
                self.results.put(('frame', self.name, path, frame))
            except Exception as e:  # noqa: BLE001 Fehler in der Oberfläche anzeigen statt den Thread zu beenden
                self.results.put(('error', self.name, path, e))


class VideoView(tk.Canvas):
    """Zeigt ein Bild seitenverhältnistreu eingepasst an."""

    def __init__(self, master):
        super().__init__(master, bg='black', highlightthickness=0, width=160, height=90)
        self._image = None
        self._photo = None
        self.bind('<Configure>', lambda e: self._redraw())

    def show(self, image):
        self._image = image
        self._redraw()

    def clear(self):
        self._image = None
        self._redraw()

    def size(self):
        return max(self.winfo_width(), 2), max(self.winfo_height(), 2)

    def _redraw(self):
        self.delete('all')
        if self._image is None:
            return
        w, h = self.size()
        scale = min(w / self._image.width, h / self._image.height)
        size = (max(1, round(self._image.width * scale)), max(1, round(self._image.height * scale)))
        image = self._image if size == self._image.size else self._image.resize(size, Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(image)
        self.create_image(w // 2, h // 2, image=self._photo)


class Timeline(tk.Canvas):
    PAD = 2

    def __init__(self, master, on_move):
        super().__init__(master, height=48, bg=COLORS['panel'], highlightthickness=1,
                         highlightbackground=COLORS['border'], highlightcolor=COLORS['accent'],
                         takefocus=1, cursor='hand2')
        self.on_move = on_move
        self.duration = 0.0
        self.frame_times = None
        self.start = 0.0
        self.length = 0.0
        self.playhead = 0.0
        self._drag_offset = None
        self.bind('<Configure>', lambda e: self.redraw())
        self.bind('<ButtonPress-1>', self._press)
        self.bind('<B1-Motion>', self._motion)
        self.bind('<ButtonRelease-1>', self._release)
        for key, step in (('<Left>', -0.1), ('<Right>', 0.1), ('<Shift-Left>', -1), ('<Shift-Right>', 1)):
            self.bind(key, lambda e, s=step: self.on_move(self.start + s))

    def _x(self, t):
        w = self.winfo_width() - 2 * self.PAD
        return self.PAD + (t / self.duration) * w if self.duration else self.PAD

    def _t(self, x):
        w = self.winfo_width() - 2 * self.PAD
        return (x - self.PAD) / w * self.duration if w > 0 else 0.0

    def redraw(self):
        self.delete('all')
        if not self.duration:
            return
        h = self.winfo_height()
        x0, x1 = self._x(self.start), self._x(self.start + self.length)
        self.create_rectangle(x0, 2, max(x1, x0 + 4), h - 2, fill=COLORS['selection'],
                              outline=COLORS['accent'], width=2)
        xp = self._x(self.playhead)
        self.create_line(xp, 0, xp, h, fill=COLORS['playhead'], width=2)

    def _press(self, event):
        self.focus_set()
        if not self.duration:
            return
        t = self._t(event.x)
        if self.start <= t <= self.start + self.length:
            self._drag_offset = t - self.start
        else:
            self._drag_offset = 0.0
            self.on_move(t)

    def _motion(self, event):
        if self._drag_offset is not None:
            self.on_move(self._t(event.x) - self._drag_offset)

    def _release(self, event):
        self._drag_offset = None


class EasyCutter:
    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        self.path = None
        self.duration = 0.0
        self.frame_times = None
        self.start = 0.0
        self.length = DEFAULT_LENGTH
        self.position = 0.0
        self.playing = False
        self.play_reader = None
        self.play_iter = None
        self.play_from = 0.0
        self.play_until = None
        self.play_clock = None
        self.exporting = False
        self._setting_scale = False

        self.workers = {
            'main': FrameWorker('main', 'at', self.results),
            'start': FrameWorker('start', 'at', self.results),
            'end': FrameWorker('end', 'before', self.results),
        }

        self._build_ui()
        self._set_enabled(False)
        self.root.after(POLL_MS, self._poll)

    # Aufbau

    def _build_ui(self):
        root = self.root
        root.title('Easy Cutter')
        root.geometry('1200x820')
        root.minsize(800, 560)
        root.configure(bg=COLORS['bg'])

        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', background=COLORS['bg'], foreground=COLORS['text'],
                        fieldbackground=COLORS['panel'], bordercolor=COLORS['border'])
        style.configure('Panel.TFrame', background=COLORS['panel'])
        style.configure('Panel.TLabel', background=COLORS['panel'])
        style.configure('Muted.TLabel', foreground=COLORS['muted'])
        style.configure('TButton', background=COLORS['panel'], padding=(12, 6))
        style.map('TButton', background=[('active', COLORS['border']), ('disabled', COLORS['bg'])],
                  foreground=[('disabled', COLORS['muted'])])
        style.configure('Accent.TButton', background=COLORS['accent'], foreground='white')
        style.map('Accent.TButton', background=[('active', '#5a9dfd'), ('disabled', COLORS['border'])])
        style.configure('TSpinbox', foreground=COLORS['text'], fieldbackground=COLORS['panel'],
                        background=COLORS['panel'], arrowcolor=COLORS['text'], insertcolor=COLORS['text'])
        style.map('TSpinbox', fieldbackground=[('disabled', COLORS['bg'])])
        style.configure('Horizontal.TScale', troughcolor=COLORS['bg'], background=COLORS['border'])
        style.configure('Horizontal.TProgressbar', background=COLORS['accent'], troughcolor=COLORS['panel'])

        header = ttk.Frame(root, padding=(16, 10))
        header.pack(fill='x')
        ttk.Label(header, text='Easy Cutter', font=('TkDefaultFont', 15, 'bold')).pack(side='left')
        ttk.Button(header, text='Video laden', command=self.open_dialog).pack(side='left', padx=16)
        self.file_label = ttk.Label(header, text='Kein Video geladen', style='Muted.TLabel')
        self.file_label.pack(side='left')

        body = ttk.Frame(root, padding=(16, 0))
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=2)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        main_panel, self.main_caption = self._panel(body, 'Video')
        main_panel.grid(row=0, column=0, rowspan=2, sticky='nsew', padx=(0, 12))
        self.main_view = VideoView(main_panel)
        self.main_view.pack(fill='both', expand=True)
        player = ttk.Frame(main_panel, style='Panel.TFrame')
        player.pack(fill='x', pady=(8, 0))
        self.play_button = ttk.Button(player, text='▶ Abspielen', width=12, command=self.toggle_play)
        self.play_button.pack(side='left')
        self.position_label = ttk.Label(player, text=format_time(0), style='Panel.TLabel', width=10)
        self.position_label.pack(side='right')
        self.scale = ttk.Scale(player, from_=0, to=1, command=self._on_scale)
        self.scale.pack(side='left', fill='x', expand=True, padx=12)

        start_panel, self.start_caption = self._panel(body, 'Erster Frame')
        start_panel.grid(row=0, column=1, sticky='nsew', pady=(0, 6))
        self.step_buttons = self._step_buttons(start_panel, self.step_start)
        self.start_view = VideoView(start_panel)
        self.start_view.pack(fill='both', expand=True)

        end_panel, self.end_caption = self._panel(body, 'Letzter Frame')
        end_panel.grid(row=1, column=1, sticky='nsew', pady=(6, 0))
        self.step_buttons += self._step_buttons(end_panel, self.step_end)
        self.end_view = VideoView(end_panel)
        self.end_view.pack(fill='both', expand=True)

        bottom = ttk.Frame(root, padding=(16, 12, 16, 16))
        bottom.pack(fill='x')
        self.timeline = Timeline(bottom, on_move=self.set_range)
        self.timeline.pack(fill='x')
        labels = ttk.Frame(bottom)
        labels.pack(fill='x')
        ttk.Label(labels, text=format_time(0), style='Muted.TLabel').pack(side='left')
        self.total_label = ttk.Label(labels, text='', style='Muted.TLabel')
        self.total_label.pack(side='right')

        controls = ttk.Frame(bottom)
        controls.pack(fill='x', pady=(10, 0))
        self.start_var = tk.StringVar(value='0.000')
        self.length_var = tk.StringVar(value=f'{DEFAULT_LENGTH:.3f}')
        self.start_spin = self._spinbox(controls, 'Start (s)', self.start_var, self._on_start_entry)
        self.length_spin = self._spinbox(controls, 'Länge (s)', self.length_var, self._on_length_entry)
        self.from_pos_button = ttk.Button(controls, text='Start = aktuelle Position',
                                          command=lambda: self.set_range(self.position))
        self.from_pos_button.pack(side='left', padx=(12, 0), anchor='s')
        self.range_button = ttk.Button(controls, text='Bereich abspielen', command=self.play_range)
        self.range_button.pack(side='left', padx=(8, 0), anchor='s')
        self.export_button = ttk.Button(controls, text='Clip speichern', style='Accent.TButton',
                                        command=self.export)
        self.export_button.pack(side='left', padx=(8, 0), anchor='s')

        self.progress = ttk.Progressbar(bottom, maximum=1.0)
        self.status = ttk.Label(bottom, text='', style='Muted.TLabel')
        self.status.pack(fill='x', pady=(10, 0))

    def _panel(self, master, title):
        frame = ttk.Frame(master, style='Panel.TFrame', padding=10)
        caption = ttk.Label(frame, text=title, style='Panel.TLabel', font=('TkDefaultFont', 10, 'bold'))
        caption.pack(anchor='w', pady=(0, 6))
        return frame, caption

    def _step_buttons(self, master, on_step):
        # Vor dem Bild packen, damit die Buttons bei wenig Platz nicht weggedrückt werden
        row = ttk.Frame(master, style='Panel.TFrame')
        row.pack(side='bottom', fill='x', pady=(8, 0))
        back = ttk.Button(row, text='◀ −1 Frame', command=lambda: on_step(-1))
        back.pack(side='left')
        forward = ttk.Button(row, text='+1 Frame ▶', command=lambda: on_step(1))
        forward.pack(side='left', padx=(8, 0))
        return [back, forward]

    def _spinbox(self, master, label, var, on_change):
        box = ttk.Frame(master)
        box.pack(side='left', padx=(0, 12))
        ttk.Label(box, text=label, style='Muted.TLabel').pack(anchor='w')
        spin = ttk.Spinbox(box, from_=0, to=10**6, increment=0.1, textvariable=var, width=10,
                           command=on_change)
        spin.pack()
        spin.bind('<Return>', lambda e: on_change())
        spin.bind('<FocusOut>', lambda e: on_change())
        return spin

    def _set_enabled(self, enabled):
        state = '!disabled' if enabled else 'disabled'
        for w in (self.play_button, self.scale, self.start_spin, self.length_spin,
                  self.from_pos_button, self.range_button):
            w.state([state])
        self.export_button.state(['!disabled' if enabled and not self.exporting else 'disabled'])
        for w in self.step_buttons:
            w.state(['!disabled' if enabled and self.frame_times else 'disabled'])

    def set_status(self, text):
        self.status.configure(text=text)

    # Video laden

    def open_dialog(self):
        path = filedialog.askopenfilename(
            title='Video laden',
            filetypes=[('Videos', '*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.wmv'), ('Alle Dateien', '*')],
        )
        if path:
            self.load(path)

    def load(self, path):
        self.pause()
        try:
            reader = VideoReader(path)
            duration = reader.duration
            reader.close()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror('Easy Cutter', f'Das Video kann nicht geöffnet werden:\n{e}')
            return
        self.path = path
        self.duration = duration
        self.frame_times = None
        threading.Thread(target=self._index_worker, args=(path,), daemon=True).start()
        self.file_label.configure(text=os.path.basename(path))
        self.total_label.configure(text=format_time(duration))
        self.timeline.duration = duration
        self._setting_scale = True
        self.scale.configure(to=duration)
        self._setting_scale = False
        for view in (self.main_view, self.start_view, self.end_view):
            view.clear()
        self.set_status('')
        self._set_enabled(True)
        self.seek(0.0)
        self.set_range(0.0, DEFAULT_LENGTH)

    # Bereich

    def set_range(self, start, length=None):
        if not self.duration:
            return
        if length is not None:
            self.length = length
        self.length = min(max(self.length, MIN_LENGTH), self.duration)
        self.start = min(max(start, 0.0), self.duration - self.length)
        self.start_var.set(f'{self.start:.3f}')
        self.length_var.set(f'{self.length:.3f}')
        self.timeline.start = self.start
        self.timeline.length = self.length
        self.timeline.redraw()
        self.workers['start'].request(self.path, self.start)
        self.workers['end'].request(self.path, self.start + self.length)

    def _on_start_entry(self):
        # Unveränderten Text nicht neu übernehmen: Die Rundung auf 3 Stellen könnte sonst einen Frame verschieben
        if self.start_var.get() == f'{self.start:.3f}':
            return
        try:
            self.set_range(float(self.start_var.get().replace(',', '.')))
        except ValueError:
            self.set_range(self.start)

    def _on_length_entry(self):
        if self.length_var.get() == f'{self.length:.3f}':
            return
        try:
            self.set_range(self.start, float(self.length_var.get().replace(',', '.')))
        except ValueError:
            self.set_range(self.start)

    def _frame_indices(self):
        """Index des ersten und des letzten Frames im aktuellen Bereich."""
        ft = self.frame_times
        first = bisect.bisect_left(ft, self.start - EPS)
        last = bisect.bisect_left(ft, self.start + self.length - EPS) - 1
        return min(first, len(ft) - 1), max(last, 0)

    def step_start(self, step):
        """Verschiebt den Anfang um `step` Frames. Das Ende bleibt stehen."""
        if not self.frame_times:
            return
        first, last = self._frame_indices()
        first = min(max(first + step, 0), last)
        end = self.start + self.length
        self.set_range(self.frame_times[first], end - self.frame_times[first])

    def step_end(self, step):
        """Verschiebt das Ende um `step` Frames. Der Anfang bleibt stehen."""
        if not self.frame_times:
            return
        ft = self.frame_times
        first, last = self._frame_indices()
        last = min(max(last + step, first), len(ft) - 1)
        # Das Ende liegt auf dem Folgeframe, der damit gerade nicht mehr zum Bereich gehört
        end = ft[last + 1] if last + 1 < len(ft) else max(self.duration, ft[last] + EPS * 10)
        self.set_range(self.start, end - self.start)

    # Hauptfenster: Position und Wiedergabe

    def _show_position(self, t):
        self.position = t
        self.position_label.configure(text=format_time(t))
        self._setting_scale = True
        self.scale.set(t)
        self._setting_scale = False
        self.timeline.playhead = t
        self.timeline.redraw()

    def _on_scale(self, value):
        if self._setting_scale or not self.path:
            return
        self.pause()
        self.seek(float(value))

    def seek(self, t):
        self._show_position(t)
        self.workers['main'].request(self.path, t)

    def toggle_play(self):
        if self.playing:
            self.pause()
        elif self.path:
            start = self.position if self.position < self.duration - 0.05 else 0.0
            self.play(start)

    def play_range(self):
        self.play(self.start, self.start + self.length)

    def play(self, start, until=None):
        self.pause()
        self.play_reader = VideoReader(self.path)
        self.play_iter = self.play_reader.frames_from(start)
        self.play_from = start
        self.play_until = until
        self.play_clock = None
        self.playing = True
        self.play_button.configure(text='⏸ Pause')
        self._play_tick()

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self.play_iter = None
        self.play_reader.close()
        self.play_reader = None
        self.play_button.configure(text='▶ Abspielen')

    def _play_tick(self):
        if not self.playing:
            return
        reader = self.play_reader
        av_frame = next(self.play_iter, None)
        # Frames vor der Startposition (ab dem Keyframe) überspringen
        while av_frame is not None and reader.time_of(av_frame) < self.play_from - EPS:
            av_frame = next(self.play_iter, None)
        if av_frame is None or (self.play_until is not None and reader.time_of(av_frame) >= self.play_until - EPS):
            self.pause()
            return
        t = reader.time_of(av_frame)
        self.main_view.show(reader.to_frame(av_frame, self.main_view.size()).image)
        self._show_position(t)
        now = time.monotonic()
        if self.play_clock is None:
            self.play_clock = (now, t)
        wall0, t0 = self.play_clock
        delay_ms = int((wall0 + (t - t0) - now) * 1000)
        self.root.after(max(delay_ms, 1), self._play_tick)

    # Export

    def export(self):
        if not self.path or self.exporting:
            return
        base = os.path.splitext(os.path.basename(self.path))[0]
        end = self.start + self.length
        stamp = lambda t: f'{t:.2f}'.replace('.', '_')  # noqa: E731
        name = f'{base}_{stamp(self.start)}-{stamp(end)}.mp4'
        output = filedialog.asksaveasfilename(
            title='Clip speichern', initialdir=os.path.dirname(self.path), initialfile=name,
            defaultextension='.mp4', filetypes=[('MP4', '*.mp4')],
        )
        if not output:
            return
        if os.path.abspath(output) == os.path.abspath(self.path):
            messagebox.showerror('Easy Cutter', 'Der Clip darf das Originalvideo nicht überschreiben.')
            return
        self.exporting = True
        self.export_button.state(['disabled'])
        self.progress['value'] = 0
        self.progress.pack(fill='x', pady=(10, 0), before=self.status)
        self.set_status('Schneide Clip …')
        args = (self.path, output, self.start, end)
        threading.Thread(target=self._export_worker, args=args, daemon=True).start()

    def _index_worker(self, path):
        try:
            self.results.put(('frame_times', path, frame_times(path)))
        except Exception as e:  # noqa: BLE001
            self.results.put(('error', 'index', path, e))

    def _export_worker(self, path, output, start, end):
        try:
            first, last = export_clip(path, output, start, end,
                                      on_progress=lambda p: self.results.put(('progress', p)))
            self.results.put(('export_done', output, first, last))
        except Exception as e:  # noqa: BLE001
            self.results.put(('export_error', e))

    def _export_finished(self):
        self.exporting = False
        self.progress.pack_forget()
        self._set_enabled(bool(self.path))

    # Ergebnisse aus Hintergrundthreads

    def _poll(self):
        try:
            while True:
                self._handle(self.results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == 'frame':
            _, name, path, frame = msg
            if path != self.path or frame is None:
                return
            if name == 'main':
                if not self.playing:
                    self.main_view.show(frame.image)
            elif name == 'start':
                self.start_view.show(frame.image)
                self.start_caption.configure(text=f'Erster Frame   {format_time(frame.time)}')
            elif name == 'end':
                self.end_view.show(frame.image)
                self.end_caption.configure(text=f'Letzter Frame   {format_time(frame.time)}')
        elif kind == 'error':
            _, name, path, err = msg
            if path == self.path:
                self.set_status(f'Fehler beim Dekodieren: {err}')
        elif kind == 'frame_times':
            _, path, times = msg
            if path == self.path and times:
                self.frame_times = times
                self._set_enabled(True)
        elif kind == 'progress':
            self.progress['value'] = msg[1]
        elif kind == 'export_done':
            _, output, first, last = msg
            self._export_finished()
            self.set_status(f'Gespeichert: {os.path.basename(output)}   '
                            f'(Frames {format_time(first)} bis {format_time(last)})')
        elif kind == 'export_error':
            self._export_finished()
            self.set_status('Export fehlgeschlagen.')
            messagebox.showerror('Easy Cutter', str(msg[1]))


def main():
    root = tk.Tk()
    app = EasyCutter(root)
    if len(sys.argv) > 1:
        root.after(100, lambda: app.load(sys.argv[1]))
    root.mainloop()
