#!/usr/bin/env python3
import sys, os, re, time, queue, threading, subprocess, platform, atexit
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# --------------------------- Models & Stats -----------------------------------

@dataclass
class Host:
    name: str
    enabled: bool = True
    window: int = 60  # samples to retain for rolling stats

@dataclass
class HostStats:
    last_rtt_ms: float | None = None
    samples: list[float | None] = field(default_factory=list)  # None for fail/timeouts
    up: bool = False
    last_change: datetime | None = None

    def push(self, success: bool, rtt_ms: float | None, window: int):
        now = datetime.now()
        self.last_rtt_ms = rtt_ms
        self.samples.append(rtt_ms if success else None)
        if len(self.samples) > window:
            self.samples = self.samples[-window:]
        prev_up = self.up
        self.up = success
        if prev_up != self.up:
            self.last_change = now

    def loss_pct(self) -> float:
        if not self.samples:
            return 0.0
        fails = sum(1 for s in self.samples if s is None)
        return round(100.0 * fails / len(self.samples), 1)

    def uptime_pct(self) -> float:
        if not self.samples:
            return 0.0
        ups = sum(1 for s in self.samples if s is not None)
        return round(100.0 * ups / len(self.samples), 1)

    def rtt_min(self) -> float | None:
        vals = [s for s in self.samples if s is not None]
        return round(min(vals), 1) if vals else None

    def rtt_max(self) -> float | None:
        vals = [s for s in self.samples if s is not None]
        return round(max(vals), 1) if vals else None

    def rtt_avg(self) -> float | None:
        vals = [s for s in self.samples if s is not None]
        return round(mean(vals), 1) if vals else None

    def jitter(self) -> float | None:
        vals = [s for s in self.samples if s is not None]
        if len(vals) < 2:
            return None
        diffs = [abs(b - a) for a, b in zip(vals, vals[1:])]
        return round(mean(diffs), 1) if diffs else None

# --------------------------- Ping runner --------------------------------------

WIN = platform.system().lower().startswith("win")
TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)

def _win_no_window_kwargs():
    """On Windows, hide console windows for subprocesses."""
    if not WIN:
        return {}
    si = subprocess.STARTUPINFO()
    # STARTF_USESHOWWINDOW == 0x00000001 ; SW_HIDE == 0
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": si,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),  # 0x08000000 fallback
    }

def ping_once(host: str, timeout_s: float) -> tuple[bool, float | None, str]:
    """Return (success, rtt_ms, raw_output) using system ping, quietly."""
    count_flag = "-n" if WIN else "-c"
    # Windows -w is timeout per reply in milliseconds; Linux/macOS -W is seconds
    if WIN:
        timeout_flag, timeout_val = "-w", str(int(max(1, timeout_s * 1000)))
    else:
        timeout_flag, timeout_val = "-W", str(int(max(1, timeout_s)))
    cmd = ["ping", count_flag, "1", timeout_flag, timeout_val, host]
    try:
        start = time.perf_counter()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 2,
            **_win_no_window_kwargs(),
        )
        raw = proc.stdout + "\n" + proc.stderr
        success = proc.returncode == 0
        rtt = None
        m = TIME_RE.search(raw)
        if m:
            rtt = float(m.group(1))
        else:
            if success:
                rtt = round((time.perf_counter() - start) * 1000.0, 1)
        return success, rtt, raw.strip()
    except subprocess.TimeoutExpired as e:
        return False, None, f"timeout: {e}"
    except Exception as e:
        return False, None, f"error: {e}"

# --------------------------- Monitor Engine -----------------------------------

class MonitorEngine:
    def __init__(self, on_result_cb, interval_s=5, timeout_s=1, max_workers=64):
        self.on_result_cb = on_result_cb
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.max_workers = max_workers
        self._hosts: dict[str, Host] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_hosts(self, hosts: list[Host]):
        self._hosts = {h.name: h for h in hosts if h.enabled}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout: float | None = None):
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def _run_loop(self):
        while not self._stop.is_set():
            names = list(self._hosts.keys())
            if not names:
                # Wait a little but check stop frequently
                for _ in range(int(self.interval_s * 10)):
                    if self._stop.is_set():
                        break
                    time.sleep(0.1)
                continue
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(names))) as ex:
                futures = {
                    ex.submit(ping_once, name, self.timeout_s): name
                    for name in names
                }
                # consume results as they arrive
                for fut in as_completed(futures, timeout=self.interval_s + self.timeout_s + 2):
                    if self._stop.is_set():
                        break
                    name = futures[fut]
                    try:
                        success, rtt, raw = fut.result()
                    except Exception as e:
                        success, rtt, raw = False, None, f"error: {e}"
                    self.on_result_cb(name, success, rtt, raw)
            # Align to interval but be responsive to stop
            for _ in range(int(self.interval_s * 10)):
                if self._stop.is_set():
                    break
                time.sleep(0.1)

# --------------------------- History Window (graphs) --------------------------

class HistoryWindow(tk.Toplevel):
    """Detached window showing a tab per host with a rolling RTT graph."""
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("Ping History")
        self.geometry("820x420")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.tabs: dict[str, tuple[ttk.Frame, tk.Canvas, tk.StringVar]] = {}
        self._running = True

        self.bind("<Destroy>", self._on_destroy)
        self.after(100, self._refresh)

    def _on_destroy(self, _evt=None):
        if _evt and _evt.widget is self:
            self._running = False

    def _refresh(self):
        if not self._running:
            return
        hosts = set(self.app.hosts.keys())
        existing = set(self.tabs.keys())
        for name in hosts - existing:
            frame = ttk.Frame(self.nb)
            top = ttk.Frame(frame); top.pack(fill="x", padx=8, pady=(8,0))
            info = tk.StringVar(value="")
            ttk.Label(top, textvariable=info, anchor="w").pack(side="left")
            canvas = tk.Canvas(frame, background="white", highlightthickness=1,
                               highlightbackground="#ddd", height=260)
            canvas.pack(fill="both", expand=True, padx=8, pady=8)
            canvas.bind("<Configure>", lambda e, n=name: self._redraw_host(n))
            self.nb.add(frame, text=name)
            self.tabs[name] = (frame, canvas, info)

        for name in existing - hosts:
            frame, _, _ = self.tabs.pop(name)
            try:
                idx = self.nb.index(frame)
                self.nb.forget(idx)
            except Exception:
                pass

        for name in hosts:
            self._redraw_host(name)

        self.after(500, self._refresh)

    def _info_text(self, st: HostStats) -> str:
        return (
            f"Status: {'UP' if st.up else 'DOWN'}   "
            f"Last: {st.last_rtt_ms if st.last_rtt_ms is not None else '-'} ms   "
            f"Loss: {st.loss_pct():.1f}%   "
            f"Uptime: {st.uptime_pct():.1f}%   "
            f"Min/Avg/Max: "
            f"{'-' if st.rtt_min() is None else st.rtt_min()} / "
            f"{'-' if st.rtt_avg() is None else st.rtt_avg()} / "
            f"{'-' if st.rtt_max() is None else st.rtt_max()} ms   "
            f"Jitter: {'-' if st.jitter() is None else st.jitter()} ms"
        )

    def _redraw_host(self, name: str):
        triple = self.tabs.get(name)
        if not triple:
            return
        frame, canvas, info = triple
        st = self.app.stats.get(name)
        w = max(10, canvas.winfo_width())
        h = max(10, canvas.winfo_height())
        canvas.delete("all")

        L, R, T, B = 40, 12, 10, 24
        plot_w, plot_h = w - L - R, h - T - B

        canvas.create_rectangle(L, T, L + plot_w, T + plot_h, outline="#e6e6e6", fill="#fafafa")
        for i in range(1, 5):
            y = T + i * plot_h / 5
            canvas.create_line(L, y, L + plot_w, y, fill="#eeeeee")

        if not st or not st.samples:
            canvas.create_text(w/2, h/2, text="No data yet…", fill="#999")
            return

        vals = [v for v in st.samples if v is not None]
        ymax = max(vals) if vals else 1.0
        if ymax <= 0: ymax = 1.0
        ymax *= 1.25

        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y_val = ymax * (1 - frac)
            y = T + plot_h * frac
            canvas.create_text(L - 6, y, text=f"{y_val:.0f}", anchor="e", fill="#777", font=("TkDefaultFont", 8))

        samples = st.samples[-len(st.samples):]
        n = len(samples)
        xs = [L + i * (plot_w / (n - 1 if n > 1 else 1)) for i in range(max(n,1))]

        last_pt = None
        for x, v in zip(xs, samples):
            if v is None:
                y = T + plot_h
                canvas.create_line(x - 4, y - 4, x + 4, y + 4, fill="#cc3333")
                canvas.create_line(x - 4, y + 4, x + 4, y - 4, fill="#cc3333")
                last_pt = None
                continue
            y = T + plot_h * (1 - min(v / ymax, 1.0))
            if last_pt is not None:
                canvas.create_line(last_pt[0], last_pt[1], x, y, fill="#2d7ef7", width=2)
            last_pt = (x, y)
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#2d7ef7", outline="")

        if vals:
            avg = sum(vals) / len(vals)
            y_avg = T + plot_h * (1 - min(avg / ymax, 1.0))
            canvas.create_line(L, y_avg, L + plot_w, y_avg, fill="#9aa7b1", dash=(3, 3))
            canvas.create_text(L + 6, y_avg - 8, text=f"avg {avg:.0f} ms", anchor="w", fill="#6b7785", font=("TkDefaultFont", 8))

        canvas.create_text(L, T + plot_h + 12, text="older", anchor="w", fill="#777", font=("TkDefaultFont", 8))
        canvas.create_text(L + plot_w, T + plot_h + 12, text="newer", anchor="e", fill="#777", font=("TkDefaultFont", 8))

        info.set(self._info_text(st))

# --------------------------- GUI ----------------------------------------------

class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.pack(fill="both", expand=True)
        self.hosts: dict[str, Host] = {}
        self.stats: dict[str, HostStats] = {}
        self.queue = queue.Queue()
        self.graphs_win: HistoryWindow | None = None
        self._after_id: str | None = None
        self._closing = False

        self._build_ui()

        self.engine = MonitorEngine(
            on_result_cb=self._on_engine_result,
            interval_s=5, timeout_s=1, max_workers=64
        )

        for h in ["1.1.1.1", "8.8.8.8", "example.com"]:
            self._add_host(h)

        self._after_id = self.after(100, self._drain_queue)

        # Ensure clean shutdown even if user Alt+F4 or clicks [X]
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        atexit.register(self._shutdown_safely)

    # ---- UI
    def _build_ui(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
        self.start_btn = ttk.Button(top, text="Start", command=self.on_start)
        self.stop_btn  = ttk.Button(top, text="Stop", command=self.on_stop, state="disabled")
        self.start_btn.pack(side="left"); self.stop_btn.pack(side="left", padx=(6,0))

        ttk.Label(top, text="Interval(s):").pack(side="left", padx=(12,4))
        self.interval_var = tk.IntVar(value=5)
        ttk.Spinbox(top, from_=1, to=60, textvariable=self.interval_var, width=4).pack(side="left")

        ttk.Label(top, text="Timeout(s):").pack(side="left", padx=(12,4))
        self.timeout_var = tk.IntVar(value=1)
        ttk.Spinbox(top, from_=1, to=10, textvariable=self.timeout_var, width=4).pack(side="left")

        ttk.Label(top, text="Parallel:").pack(side="left", padx=(12,4))
        self.parallel_var = tk.IntVar(value=64)
        ttk.Spinbox(top, from_=1, to=512, textvariable=self.parallel_var, width=5).pack(side="left")

        ttk.Button(top, text="Add", command=self.on_add).pack(side="right")
        ttk.Button(top, text="Remove", command=self.on_remove).pack(side="right", padx=(0,6))
        ttk.Button(top, text="Graphs", command=self.on_graphs).pack(side="right", padx=(0,6))

        cols = ("host","status","last_rtt","loss","uptime","last_change","min","avg","max","jitter")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=16)
        headings = {
            "host":"Host", "status":"Status", "last_rtt":"Last RTT",
            "loss":"Loss%", "uptime":"Uptime%", "last_change":"Last Change",
            "min":"Min", "avg":"Avg", "max":"Max", "jitter":"Jitter"
        }
        for k, title in headings.items():
            self.tree.heading(k, text=title)
            self.tree.column(k, width=90 if k!="host" else 180, anchor="center")
        self.tree.column("host", anchor="w", width=190)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0,8))

        self.style = ttk.Style(self)
        self.tree.tag_configure("UP", background="#e9f7ef")
        self.tree.tag_configure("DOWN", background="#fdecea")
        self.tree.tag_configure("FLAP", background="#fff4e5")

        self.status = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0,6))

    # ---- Host mgmt
    def _add_host(self, name: str):
        if name in self.hosts:
            return
        host = Host(name=name)
        self.hosts[name] = host
        self.stats[name] = HostStats()
        self.tree.insert("", "end", iid=name, values=(name,"-","-","-","-","-","-","-","-","-"))

    def _remove_selected(self):
        for iid in self.tree.selection():
            name = iid
            self.hosts.pop(name, None)
            self.stats.pop(name, None)
            self.tree.delete(iid)

    # ---- Engine callbacks & UI updates
    def _on_engine_result(self, name: str, success: bool, rtt: float | None, raw: str):
        self.queue.put(("result", name, success, rtt, raw))

    def _drain_queue(self):
        if self._closing:
            return
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg[0] == "result":
                    _, name, success, rtt, raw = msg
                    st = self.stats.get(name)
                    h = self.hosts.get(name)
                    if st and h:
                        st.push(success, rtt, h.window)
                        self._update_row(name, st)
        except queue.Empty:
            pass
        self._after_id = self.after(100, self._drain_queue)

    def _update_row(self, name: str, st: HostStats):
        def fmt(x, unit=" ms"):
            if x is None: return "-"
            return f"{x}{unit}" if isinstance(x, (int,float)) else str(x)

        status = "UP" if st.up else "DOWN"
        recent = st.samples[-5:]
        if any(s is None for s in recent) and any(s is not None for s in recent):
            status = "FLAP"

        values = (
            name,
            status,
            fmt(st.last_rtt_ms),
            f"{st.loss_pct():.1f}",
            f"{st.uptime_pct():.1f}",
            st.last_change.strftime("%Y-%m-%d %H:%M:%S") if st.last_change else "-",
            fmt(st.rtt_min()),
            fmt(st.rtt_avg()),
            fmt(st.rtt_max()),
            fmt(st.jitter())
        )
        if name in self.tree.get_children(""):
            self.tree.item(name, values=values, tags=(status,))
        else:
            self.tree.insert("", "end", iid=name, values=values, tags=(status,))
        self.status.set(f"Last update: {datetime.now().strftime('%H:%M:%S')}")

    # ---- UI event handlers
    def on_start(self):
        self.engine.interval_s = max(1, int(self.interval_var.get()))
        self.engine.timeout_s  = max(1, int(self.timeout_var.get()))
        self.engine.max_workers = max(1, int(self.parallel_var.get()))
        self.engine.set_hosts(list(self.hosts.values()))
        self.engine.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set(f"Monitoring {len(self.hosts)} hosts… every {self.engine.interval_s}s")

    def on_stop(self):
        self.engine.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set("Stopped")

    def on_add(self):
        name = simpledialog.askstring("Add Host", "Hostname or IP:")
        if name:
            self._add_host(name.strip())

    def on_remove(self):
        if not self.tree.selection():
            messagebox.showinfo("Remove", "Select rows to remove.")
            return
        self._remove_selected()

    def on_graphs(self):
        if self.graphs_win and self.graphs_win.winfo_exists():
            self.graphs_win.lift()
            return
        self.graphs_win = HistoryWindow(self)

    def on_close(self):
        """Close button/X: stop engine, cancel timers, close graphs, exit."""
        if self._closing:
            return
        self._closing = True
        try:
            if self._after_id:
                try:
                    self.after_cancel(self._after_id)
                except Exception:
                    pass
            self.engine.stop()
            self.engine.join(timeout=3.0)
            if self.graphs_win and self.graphs_win.winfo_exists():
                self.graphs_win._running = False
                try:
                    self.graphs_win.destroy()
                except Exception:
                    pass
        finally:
            self.master.destroy()

    def _shutdown_safely(self):
        """Safety net if process exits unexpectedly."""
        try:
            self.engine.stop()
            self.engine.join(timeout=1.0)
        except Exception:
            pass

# --------------------------- Main ---------------------------------------------

def main():
    root = tk.Tk()
    root.title("Ping Monitor")
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # per-monitor DPI on Win
    except Exception:
        pass
    app = App(root)
    root.minsize(900, 520)
    root.mainloop()

if __name__ == "__main__":
    main()
