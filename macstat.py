#!/usr/bin/env python3
"""macstat — terminal dashboard for macOS system state.

Shows CPU load, per-core usage, memory, swap, thermal/throttling state,
battery, disk, network throughput, and top processes in a live TUI.
Run: ./macstat.py     (Ctrl-C to exit)
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

import psutil
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

REFRESH = 2.0           # seconds between full panel updates
PROC_EVERY = 3          # only iterate processes every N renders (~6s)
DISK_EVERY = 15         # only re-scan disks every N renders (~30s)
POWER_INTERVAL_MS = 3000  # powermetrics sampling interval
HIST_LEN = 60           # samples per sparkline (~2 minutes at 2s refresh)

SPARK_CHARS = "▁▂▃▄▅▆▇█"


class History:
    """Fixed-length ring buffer with sparkline render helper."""

    def __init__(self, length: int = HIST_LEN) -> None:
        self.length = length
        self.values: list[float] = []

    def push(self, v: float | None) -> None:
        if v is None:
            return
        self.values.append(float(v))
        if len(self.values) > self.length:
            del self.values[: len(self.values) - self.length]

    def spark(self, width: int = 30, lo: float | None = None,
              hi: float | None = None) -> Text:
        if not self.values:
            return Text("─" * width, style="grey37")
        data = self.values[-width:]
        if lo is None:
            lo = min(data)
        if hi is None:
            hi = max(data)
        if hi - lo < 1e-9:
            hi = lo + 1.0
        out = Text()
        for v in data:
            idx = int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))
            idx = max(0, min(len(SPARK_CHARS) - 1, idx))
            out.append(SPARK_CHARS[idx], style=color_for_pct((v - lo) / (hi - lo) * 100))
        if len(data) < width:
            out = Text(" " * (width - len(data)), style="grey37") + out
        return out


HIST: dict[str, History] = {
    "cpu": History(),
    "ram": History(),
    "soc_w": History(),
    "cpu_temp": History(),
    "net_rx": History(),
    "net_tx": History(),
}


def color_for_pct(p: float) -> str:
    if p >= 90:
        return "bold red"
    if p >= 75:
        return "red"
    if p >= 50:
        return "yellow"
    if p >= 25:
        return "green"
    return "bright_green"


def bar(pct: float, width: int = 20) -> Text:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    style = color_for_pct(pct)
    t = Text()
    t.append("█" * filled, style=style)
    t.append("░" * (width - filled), style="grey37")
    return t


def fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:6.1f}{unit}"
        n /= 1024
    return f"{n:6.1f}P"


def fmt_rate(n: float) -> str:
    return f"{fmt_bytes(n)}/s"


def read_thermal() -> dict:
    """Parse `pmset -g therm` — works without sudo on Apple Silicon & Intel.

    Returns speed_limit (0–100) when reported. When pmset only says
    'no warning level has been recorded', we treat that as nominal (100).
    """
    out = {"speed_limit": None, "raw": "", "available": False, "nominal": False}
    try:
        r = subprocess.run(
            ["pmset", "-g", "therm"], capture_output=True, text=True, timeout=2
        )
        out["raw"] = r.stdout.strip()
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", r.stdout)
        if m:
            out["speed_limit"] = int(m.group(1))
            out["available"] = True
        elif "No thermal warning level" in r.stdout or "no thermal" in r.stdout.lower():
            out["speed_limit"] = 100
            out["available"] = True
            out["nominal"] = True
    except Exception:
        pass
    return out


def get_cpu_freq_mhz() -> float | None:
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            return f.current
    except Exception:
        pass
    return None


def header_panel() -> Panel:
    host = socket.gethostname()
    uptime = timedelta(seconds=int(time.time() - psutil.boot_time()))
    load1, load5, load15 = os.getloadavg()
    cores = psutil.cpu_count(logical=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    t.add_column(justify="center")
    t.add_column(justify="right")
    load_color = color_for_pct((load1 / cores) * 100)
    t.add_row(
        Text.assemble(("host  ", "grey50"), (host, "bold cyan")),
        Text.assemble(
            ("load  ", "grey50"),
            (f"{load1:.2f}", load_color),
            (f"  {load5:.2f}  {load15:.2f}", "white"),
            ("  /", "grey50"),
            (f" {cores} cores", "grey50"),
        ),
        Text.assemble(("up  ", "grey50"), (str(uptime), "bold"), (f"   {now}", "grey50")),
    )
    return Panel(t, border_style="grey30", padding=(0, 1))


def cpu_panel(per_core: list[float]) -> Panel:
    total = sum(per_core) / len(per_core) if per_core else 0.0
    HIST["cpu"].push(total)
    freq = get_cpu_freq_mhz()
    therm = read_thermal()

    head = Table.grid(expand=True)
    head.add_column(ratio=1)
    head.add_column(justify="right")
    total_line = Text.assemble(
        ("Total ", "bold"),
        bar(total, 30),
        (f"  {total:5.1f}%", color_for_pct(total)),
        ("  ", "grey50"),
        HIST["cpu"].spark(width=30, lo=0, hi=100),
    )
    extras = Text()
    if freq:
        extras.append(f"freq {freq/1000:.2f}GHz  ", style="grey62")
    if therm["available"]:
        sl = therm["speed_limit"]
        if sl is not None:
            sl_style = "bright_green" if sl >= 100 else ("yellow" if sl >= 70 else "red")
            if therm.get("nominal") and sl >= 100:
                extras.append("thermal nominal", style=sl_style)
            else:
                throttled = "" if sl >= 100 else "  THROTTLED"
                extras.append(f"speed-limit {sl}%{throttled}", style=sl_style)
    head.add_row(total_line, extras)

    grid = Table.grid(expand=True, padding=(0, 2))
    cols = 4 if len(per_core) >= 8 else 2
    for _ in range(cols):
        grid.add_column()
    row: list[Text] = []
    for i, p in enumerate(per_core):
        cell = Text.assemble(
            (f"c{i:<2} ", "grey62"),
            bar(p, 14),
            (f" {p:5.1f}%", color_for_pct(p)),
        )
        row.append(cell)
        if len(row) == cols:
            grid.add_row(*row)
            row = []
    if row:
        while len(row) < cols:
            row.append(Text(""))
        grid.add_row(*row)

    return Panel(Group(head, Text(""), grid), title="CPU", border_style="cyan")


def mem_panel() -> Panel:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    HIST["ram"].push(vm.percent)
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=8)
    t.add_column(ratio=1)
    t.add_column(justify="right")
    t.add_row(
        Text("RAM", style="bold"),
        bar(vm.percent, 30),
        Text(f"{fmt_bytes(vm.used)} / {fmt_bytes(vm.total)}  {vm.percent:.0f}%",
             style=color_for_pct(vm.percent)),
    )
    # macOS-specific breakdown if available
    extras_parts = []
    for attr, label in [("active", "active"), ("wired", "wired"),
                        ("inactive", "inactive"), ("available", "avail")]:
        if hasattr(vm, attr):
            extras_parts.append(f"{label} {fmt_bytes(getattr(vm, attr)).strip()}")
    if extras_parts:
        t.add_row(Text(""), Text("  ".join(extras_parts), style="grey62"), Text(""))
    swap_pct = sm.percent
    t.add_row(
        Text("Swap", style="bold"),
        bar(swap_pct, 30),
        Text(f"{fmt_bytes(sm.used)} / {fmt_bytes(sm.total)}  {swap_pct:.0f}%",
             style=color_for_pct(swap_pct)),
    )
    # Memory pressure + sparkline
    level = read_mem_pressure()
    p_label, p_style = {
        1: ("Normal",   "bright_green"),
        2: ("Warning",  "yellow"),
        4: ("Critical", "bold red"),
    }.get(level if level is not None else -1, ("?", "grey50"))
    spark = HIST["ram"].spark(width=30, lo=0, hi=100)
    t.add_row(
        Text("Pressure", style="bold"),
        spark,
        Text(p_label, style=p_style),
    )
    return Panel(t, title="Memory", border_style="magenta")


_disk_parts_cache: dict = {"parts": None, "ts": 0.0}


def disk_panel(refresh_parts: bool = True) -> Panel:
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=14, overflow="ellipsis")
    t.add_column(ratio=1)
    t.add_column(justify="right")
    if refresh_parts or _disk_parts_cache["parts"] is None:
        _disk_parts_cache["parts"] = psutil.disk_partitions(all=False)
        _disk_parts_cache["ts"] = time.time()
    # I/O rate header row
    rr, wr = read_disk_io()
    t.add_row(
        Text("I/O", style="bold"),
        Text.assemble(
            ("⬇ ", "green"), (fmt_rate(rr), "green"),
            ("    ", "grey50"),
            ("⬆ ", "yellow"), (fmt_rate(wr), "yellow"),
        ),
        Text(""),
    )
    seen = set()
    for part in _disk_parts_cache["parts"]:
        if part.mountpoint in seen:
            continue
        seen.add(part.mountpoint)
        try:
            u = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        mp = part.mountpoint if len(part.mountpoint) <= 14 else "…" + part.mountpoint[-13:]
        t.add_row(
            Text(mp, style="grey78"),
            bar(u.percent, 24),
            Text(f"{fmt_bytes(u.used)} / {fmt_bytes(u.total)}  {u.percent:.0f}%",
                 style=color_for_pct(u.percent)),
        )
    return Panel(t, title="Disk", border_style="blue")


class PowerMetricsReader:
    """Background stream of `sudo -n powermetrics` parsing power + thermal pressure.

    Apple Silicon doesn't expose die temperatures via powermetrics, so we
    surface what *is* available: CPU/GPU/ANE power and thermal pressure level.
    """

    LINE_RE = {
        "cpu_power": re.compile(r"^CPU Power:\s*([0-9.]+)\s*mW"),
        "gpu_power": re.compile(r"^GPU Power:\s*([0-9.]+)\s*mW"),
        "ane_power": re.compile(r"^ANE Power:\s*([0-9.]+)\s*mW"),
        "combined":  re.compile(r"Combined Power.*?:\s*([0-9.]+)\s*mW"),
        "pressure":  re.compile(r"Current pressure level:\s*(\w+)"),
        "e_freq":    re.compile(r"E-Cluster HW active frequency:\s*([0-9]+)\s*MHz"),
        "p_freq":    re.compile(r"P0-Cluster HW active frequency:\s*([0-9]+)\s*MHz"),
        "gpu_freq":  re.compile(r"GPU HW active frequency:\s*([0-9]+)\s*MHz"),
    }

    def __init__(self, interval_ms: int = 2000) -> None:
        self.interval_ms = interval_ms
        self.latest: dict = {k: None for k in self.LINE_RE}
        self.latest["ts"] = 0.0
        self.status = "starting"  # starting | ok | no-sudo | error
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["sudo", "-n", "/usr/bin/powermetrics",
                 "--samplers", "cpu_power,gpu_power,ane_power,thermal",
                 "-i", str(self.interval_ms)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self.status = "error"
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            for line in self._proc.stdout:
                if self._stop:
                    break
                for key, pat in self.LINE_RE.items():
                    m = pat.match(line) if pat.pattern.startswith("^") else pat.search(line)
                    if not m:
                        continue
                    val = m.group(1)
                    self.latest[key] = val if key == "pressure" else float(val)
                    self.latest["ts"] = time.time()
                    if self.status != "ok":
                        self.status = "ok"
                    break
        except Exception:
            pass
        if self._proc and self._proc.poll() is not None and self.status == "starting":
            self.status = "no-sudo"

    def stop(self) -> None:
        self._stop = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass


def fmt_watts(mw: float | None) -> str:
    if mw is None:
        return "—"
    if mw >= 1000:
        return f"{mw/1000:5.2f} W"
    return f"{mw:5.0f} mW"


def power_color(mw: float | None, high_mw: float) -> str:
    if mw is None:
        return "grey50"
    pct = min(100.0, mw / high_mw * 100)
    return color_for_pct(pct)


def power_panel(reader: PowerMetricsReader) -> Panel:
    if reader.status == "no-sudo":
        return Panel(Align.center(Text(
            "needs NOPASSWD sudo for /usr/bin/powermetrics", style="yellow")),
            title="Power & Thermal", border_style="yellow")
    if reader.status == "error":
        return Panel(Align.center(Text("powermetrics unavailable", style="red")),
                     title="Power & Thermal", border_style="red")
    if reader.status == "starting" or reader.latest["ts"] == 0:
        return Panel(Align.center(Text("sampling…", style="grey62")),
                     title="Power & Thermal", border_style="grey50")

    L = reader.latest
    cpu_p = L.get("cpu_power")
    gpu_p = L.get("gpu_power")
    ane_p = L.get("ane_power")
    combined = L.get("combined")
    pressure = L.get("pressure")
    e_freq = L.get("e_freq")
    p_freq = L.get("p_freq")
    gpu_freq = L.get("gpu_freq")

    # Approximate ceilings for bar scaling on M2 Pro
    CEIL_CPU = 30000  # 30W is plenty headroom
    CEIL_GPU = 25000
    CEIL_ANE = 8000
    CEIL_COMB = 50000

    def row(label: str, mw: float | None, ceil: float) -> tuple:
        pct = 0.0 if mw is None else min(100.0, mw / ceil * 100)
        return (
            Text(label, style="bold"),
            bar(pct, 22),
            Text(fmt_watts(mw), style=power_color(mw, ceil)),
        )

    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=8)
    t.add_column(ratio=1)
    t.add_column(justify="right", width=10)
    t.add_row(*row("CPU", cpu_p, CEIL_CPU))
    t.add_row(*row("GPU", gpu_p, CEIL_GPU))
    t.add_row(*row("ANE", ane_p, CEIL_ANE))
    t.add_row(*row("SoC", combined, CEIL_COMB))
    if combined is not None:
        HIST["soc_w"].push(combined / 1000.0)
        t.add_row(
            Text("trend", style="grey62"),
            HIST["soc_w"].spark(width=22),
            Text("60s", style="grey50"),
        )

    # Power source (battery + adapter) from ioreg
    src = read_battery_source()
    bw = src.get("battery_w")
    sys_w = src.get("system_load_w")
    adapter_w = src.get("adapter_max_w")
    external = src.get("external")
    charging = src.get("is_charging")

    src_table = Table.grid(expand=True, padding=(0, 1))
    src_table.add_column(width=8)
    src_table.add_column(ratio=1)
    src_table.add_column(justify="right", width=10)

    # DCin (actual wall input): energy balance — DCin = SystemLoad + Battery_signed
    dcin_w = None
    if external and sys_w is not None and bw is not None:
        dcin_w = max(0.0, sys_w + bw)
    if external and dcin_w is not None:
        cap = adapter_w if adapter_w else 100
        pct = min(100.0, dcin_w / cap * 100)
        suffix = f" / {adapter_w}W" if adapter_w else ""
        src_table.add_row(
            Text("DCin", style="bold cyan"),
            bar(pct, 20),
            Text(f"{dcin_w:5.1f} W{suffix}", style=color_for_pct(pct)),
        )
    elif external is False:
        src_table.add_row(
            Text("DCin", style="bold grey50"),
            Text("on battery", style="grey50"),
            Text("—", style="grey50"),
        )

    # Battery: signed power; + charging, - discharging
    if bw is not None:
        if abs(bw) < 0.05:
            label = "idle"
            bw_style = "grey62"
            arrow = ""
        elif bw > 0:
            label = "charging"
            bw_style = "bright_green"
            arrow = "↑ "
        else:
            label = "discharging"
            bw_style = "yellow"
            arrow = "↓ "
        src_table.add_row(
            Text("Battery", style="bold"),
            Text(label, style=bw_style),
            Text(f"{arrow}{abs(bw):4.2f} W", style=bw_style),
        )

    # System load (from ioreg SystemLoad)
    if sys_w is not None:
        src_table.add_row(
            Text("System", style="bold"),
            bar(min(100.0, sys_w / 50.0 * 100), 20),
            Text(f"{sys_w:4.2f} W", style=color_for_pct(min(100, sys_w / 50.0 * 100))),
        )

    # frequency + pressure footer
    foot = Text()
    parts = []
    if e_freq:
        parts.append(("E", f"{int(e_freq)}MHz"))
    if p_freq:
        parts.append(("P", f"{int(p_freq)}MHz"))
    if gpu_freq:
        parts.append(("GPU", f"{int(gpu_freq)}MHz"))
    for i, (k, v) in enumerate(parts):
        if i:
            foot.append("  ", style="grey50")
        foot.append(f"{k} ", style="grey62")
        foot.append(v, style="white")

    if pressure:
        p_style = {"Nominal": "bright_green", "Light": "yellow",
                   "Moderate": "yellow", "Heavy": "red", "Trapping": "bold red",
                   "Sleeping": "grey62"}.get(pressure, "white")
        if foot.plain:
            foot.append("   ", style="grey50")
        foot.append("pressure ", style="grey62")
        foot.append(pressure, style=p_style)

    # Temperatures (HID sensors)
    temps = read_temperatures()
    temp_table = Table.grid(expand=True, padding=(0, 1))
    temp_table.add_column(width=8)
    temp_table.add_column(ratio=1)
    temp_table.add_column(justify="right", width=10)

    def temp_row(label: str, c: float | None) -> None:
        if c is None:
            return
        pct = max(0.0, min(100.0, (c - 30) / 70 * 100))  # scale 30..100°C
        temp_table.add_row(
            Text(label, style="bold"),
            bar(pct, 20),
            Text(f"{c:5.1f} °C", style=temp_color(c)),
        )

    temp_row("CPU", temps.get("cpu"))
    if temps.get("cpu") is not None:
        HIST["cpu_temp"].push(temps["cpu"])
        temp_table.add_row(
            Text("trend", style="grey62"),
            HIST["cpu_temp"].spark(width=22),
            Text("60s", style="grey50"),
        )
    temp_row("GPU", temps.get("gpu"))
    temp_row("Hottest", temps.get("soc_max"))
    if temps.get("battery") is not None:
        temp_table.add_row(
            Text("Battery", style="grey78"),
            Text("", ),
            Text(f"{temps['battery']:5.1f} °C", style=temp_color(temps['battery'], hot=50, warm=40)),
        )
    if temps.get("ssd") is not None:
        temp_table.add_row(
            Text("SSD", style="grey78"),
            Text("", ),
            Text(f"{temps['ssd']:5.1f} °C", style=temp_color(temps['ssd'], hot=70, warm=55)),
        )

    has_src = bool(src_table.row_count)
    has_temps = bool(temp_table.row_count)
    parts = [t]
    if has_src:
        parts += [Text(""), src_table]
    if has_temps:
        parts += [Text(""), temp_table]
    if foot.plain:
        parts += [Text(""), foot]
    body = Group(*parts) if len(parts) > 1 else t
    border = "bright_red" if pressure in ("Heavy", "Trapping") else \
             ("yellow" if pressure in ("Light", "Moderate") else "red")
    return Panel(body, title="Power & Thermal", border_style=border)


_batt_src_cache: dict = {"ts": 0.0, "data": {}}
BATT_TTL = 3.0

SENSORS_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "macstat-sensors")
_temp_cache: dict = {"ts": 0.0, "raw": {}, "agg": {}, "available": None}
TEMP_TTL = 4.0


def read_temperatures() -> dict:
    """Run the HID temperature helper and return aggregated readings.

    Returns: {cpu, gpu, battery, ssd, soc_max, raw}  (all °C or None)
    """
    if _temp_cache["available"] is False:
        return _temp_cache["agg"]
    if time.time() - _temp_cache["ts"] < TEMP_TTL and _temp_cache["agg"]:
        return _temp_cache["agg"]
    if not os.path.exists(SENSORS_BIN):
        _temp_cache["available"] = False
        return {}
    try:
        r = subprocess.run([SENSORS_BIN], capture_output=True, text=True, timeout=2)
        import json
        raw = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        _temp_cache["available"] = False
        return {}

    def avg(xs: list) -> float | None:
        return sum(xs) / len(xs) if xs else None

    cpu_vals: list[float] = []
    gpu_vals: list[float] = []
    soc_vals: list[float] = []  # all PMU sensors (overall SoC heat)
    battery: float | None = None
    ssd_vals: list[float] = []

    for k, v in raw.items():
        if not isinstance(v, (int, float)):
            continue
        key = k.strip()
        kl = key.lower()
        if key.startswith("PMU tdie") or (key.startswith("PMU TP") and key.endswith("s")):
            cpu_vals.append(v)
            soc_vals.append(v)
        elif key.startswith("PMU TP") and key.endswith("g"):
            gpu_vals.append(v)
            soc_vals.append(v)
        elif key.startswith("PMU "):
            soc_vals.append(v)
        elif "battery" in kl:
            battery = v
        elif "nand" in kl or "ssd" in kl:
            ssd_vals.append(v)

    agg = {
        "cpu": avg(cpu_vals),
        "gpu": avg(gpu_vals),
        "soc_max": max(soc_vals) if soc_vals else None,
        "battery": battery,
        "ssd": avg(ssd_vals),
    }
    _temp_cache["ts"] = time.time()
    _temp_cache["raw"] = raw
    _temp_cache["agg"] = agg
    _temp_cache["available"] = True
    return agg


def temp_color(c: float | None, hot: float = 85, warm: float = 70) -> str:
    if c is None:
        return "grey50"
    if c >= hot:
        return "bold red"
    if c >= warm:
        return "yellow"
    if c >= 55:
        return "green"
    return "bright_green"


def _signed64(v: int | None) -> int | None:
    if v is None:
        return None
    if v > 2**32:  # ioreg renders 64-bit signed negatives as huge positives
        v -= 2**64
    return v


def read_battery_source() -> dict:
    """Read battery & adapter power state from AppleSmartBattery via ioreg.

    Returns: {voltage_v, amperage_a, battery_w (signed, +=charging),
              is_charging, external, adapter_max_w, system_load_w}
    Cached for BATT_TTL seconds (ioreg ≈ 27ms).
    """
    if time.time() - _batt_src_cache["ts"] < BATT_TTL and _batt_src_cache["data"]:
        return _batt_src_cache["data"]
    data: dict = {"voltage_v": None, "amperage_a": None, "battery_w": None,
                  "is_charging": None, "external": None, "adapter_max_w": None,
                  "system_load_w": None}
    try:
        r = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"],
                           capture_output=True, text=True, timeout=2)
        text = r.stdout
    except Exception:
        return data

    def find_int(key: str) -> int | None:
        m = re.search(rf'"{key}"\s*=\s*(-?\d+)', text)
        return int(m.group(1)) if m else None

    def find_bool(key: str) -> bool | None:
        m = re.search(rf'"{key}"\s*=\s*(Yes|No)', text)
        return (m.group(1) == "Yes") if m else None

    voltage_mv = find_int("Voltage")
    amperage_ma = _signed64(find_int("Amperage"))
    data["is_charging"] = find_bool("IsCharging")
    data["external"] = find_bool("ExternalConnected")
    data["voltage_v"] = voltage_mv / 1000 if voltage_mv else None
    data["amperage_a"] = amperage_ma / 1000 if amperage_ma is not None else None
    if voltage_mv and amperage_ma is not None:
        data["battery_w"] = voltage_mv * amperage_ma / 1_000_000.0

    m = re.search(r'"AdapterDetails"\s*=\s*\{[^}]*"Watts"=(\d+)', text)
    if m:
        data["adapter_max_w"] = int(m.group(1))

    # SystemLoad inside BatteryData blob — appears to be in mW on Apple Silicon
    m = re.search(r'"SystemLoad"=(\d+)', text)
    if m:
        data["system_load_w"] = int(m.group(1)) / 1000.0

    _batt_src_cache["data"] = data
    _batt_src_cache["ts"] = time.time()
    return data


_mem_pressure_cache: dict = {"ts": 0.0, "level": None}


def read_mem_pressure() -> int | None:
    """Return macOS memory pressure level: 1=Normal, 2=Warning, 4=Critical."""
    if time.time() - _mem_pressure_cache["ts"] < 3.0 and _mem_pressure_cache["level"] is not None:
        return _mem_pressure_cache["level"]
    try:
        r = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=1,
        )
        v = int(r.stdout.strip())
        _mem_pressure_cache["level"] = v
        _mem_pressure_cache["ts"] = time.time()
        return v
    except Exception:
        return None


_wifi_iface_cache: dict = {"iface": None, "ts": 0.0}


def find_wifi_iface() -> str | None:
    """Find the Wi-Fi BSD interface name (cached for 30s)."""
    if time.time() - _wifi_iface_cache["ts"] < 30.0 and _wifi_iface_cache["iface"]:
        return _wifi_iface_cache["iface"]
    try:
        r = subprocess.run(["networksetup", "-listallhardwareports"],
                           capture_output=True, text=True, timeout=2)
        # blocks: "Hardware Port: Wi-Fi\nDevice: enX"
        m = re.search(r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*(\w+)", r.stdout)
        if m:
            _wifi_iface_cache["iface"] = m.group(1)
            _wifi_iface_cache["ts"] = time.time()
            return m.group(1)
    except Exception:
        pass
    return None


_wifi_info_cache: dict = {"ts": 0.0, "data": {}}


def read_wifi_info() -> dict:
    """SSID, IP, RSSI, link rate for the Wi-Fi interface. Cached 5s."""
    if time.time() - _wifi_info_cache["ts"] < 5.0 and _wifi_info_cache["data"]:
        return _wifi_info_cache["data"]
    info: dict = {"iface": None, "ssid": None, "ip": None,
                  "rssi": None, "rate": None, "channel": None}
    iface = find_wifi_iface()
    info["iface"] = iface
    if not iface:
        _wifi_info_cache["data"] = info
        _wifi_info_cache["ts"] = time.time()
        return info
    try:
        addrs = psutil.net_if_addrs().get(iface, [])
        for a in addrs:
            if a.family == socket.AF_INET:
                info["ip"] = a.address
                break
    except Exception:
        pass
    try:
        r = subprocess.run(["ipconfig", "getsummary", iface],
                           capture_output=True, text=True, timeout=1)
        out = r.stdout
        m = re.search(r"SSID\s*:\s*(\S.*)", out)
        if m:
            info["ssid"] = m.group(1).strip()
        m = re.search(r"RSSI\s*:\s*(-?\d+)", out)
        if m:
            info["rssi"] = int(m.group(1))
        m = re.search(r"(?:LinkSpeed|RxRate|TxRate)\s*:\s*([\d.]+)", out)
        if m:
            info["rate"] = float(m.group(1))
        m = re.search(r"Channel\s*:\s*(\d+)", out)
        if m:
            info["channel"] = int(m.group(1))
    except Exception:
        pass
    _wifi_info_cache["data"] = info
    _wifi_info_cache["ts"] = time.time()
    return info


_last_disk_io = {"t": 0.0, "r": 0, "w": 0}


def read_disk_io() -> tuple[float, float]:
    """Return current (read_rate, write_rate) in bytes/sec across all disks."""
    try:
        io = psutil.disk_io_counters()
    except Exception:
        return 0.0, 0.0
    if io is None:
        return 0.0, 0.0
    now = time.time()
    rate_r = rate_w = 0.0
    if _last_disk_io["t"]:
        dt = max(now - _last_disk_io["t"], 1e-6)
        rate_r = (io.read_bytes - _last_disk_io["r"]) / dt
        rate_w = (io.write_bytes - _last_disk_io["w"]) / dt
    _last_disk_io.update(t=now, r=io.read_bytes, w=io.write_bytes)
    return rate_r, rate_w


_last_net = {"t": 0.0, "rx": 0, "tx": 0}


def net_panel() -> Panel:
    nio = psutil.net_io_counters()
    now = time.time()
    rx_rate = tx_rate = 0.0
    if _last_net["t"]:
        dt = max(now - _last_net["t"], 1e-6)
        rx_rate = (nio.bytes_recv - _last_net["rx"]) / dt
        tx_rate = (nio.bytes_sent - _last_net["tx"]) / dt
    _last_net.update(t=now, rx=nio.bytes_recv, tx=nio.bytes_sent)
    HIST["net_rx"].push(rx_rate)
    HIST["net_tx"].push(tx_rate)

    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=6)
    t.add_column(justify="right", width=12)
    t.add_column(ratio=1)
    t.add_row(
        Text("⬇ rx", style="bold green"),
        Text(fmt_rate(rx_rate), style="green"),
        HIST["net_rx"].spark(width=18),
    )
    t.add_row(
        Text("⬆ tx", style="bold yellow"),
        Text(fmt_rate(tx_rate), style="yellow"),
        HIST["net_tx"].spark(width=18),
    )
    t.add_row(
        Text("total", style="grey62"),
        Text(fmt_bytes(nio.bytes_recv), style="grey78"),
        Text(f"sent {fmt_bytes(nio.bytes_sent).strip()}", style="grey62"),
    )
    return Panel(t, title="Network", border_style="green")


def wifi_panel() -> Panel:
    info = read_wifi_info()
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(width=8)
    t.add_column(ratio=1)
    if not info.get("iface"):
        return Panel(Align.center(Text("no Wi-Fi interface", style="grey50")),
                     title="Wi-Fi", border_style="grey30")
    ssid = info.get("ssid")
    if ssid in (None, "", "<redacted>"):
        ssid_text = Text("<redacted>", style="grey62")
    else:
        ssid_text = Text(ssid, style="bold cyan")
    t.add_row(Text("SSID", style="bold"), ssid_text)
    if info.get("ip"):
        t.add_row(Text("IP", style="grey62"), Text(info["ip"], style="white"))
    extras = []
    if info.get("rssi") is not None:
        rssi = info["rssi"]
        rs_style = ("bright_green" if rssi >= -55 else
                    "green" if rssi >= -67 else
                    "yellow" if rssi >= -75 else "red")
        extras.append(("RSSI", f"{rssi} dBm", rs_style))
    if info.get("rate"):
        extras.append(("Rate", f"{info['rate']:.0f} Mbps", "white"))
    if info.get("channel"):
        extras.append(("Ch", str(info["channel"]), "grey78"))
    if extras:
        line = Text()
        for i, (k, v, s) in enumerate(extras):
            if i:
                line.append("   ", style="grey50")
            line.append(f"{k} ", style="grey62")
            line.append(v, style=s)
        t.add_row(Text(""), line)
    return Panel(t, title=f"Wi-Fi ({info['iface']})", border_style="cyan")


def battery_panel() -> Panel | None:
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is None:
        return None
    pct = b.percent
    plugged = b.power_plugged
    status = "charging" if plugged else "on battery"
    secs = b.secsleft
    if secs == psutil.POWER_TIME_UNLIMITED or plugged:
        remain = "—"
    elif secs == psutil.POWER_TIME_UNKNOWN or secs is None or secs < 0:
        remain = "?"
    else:
        remain = str(timedelta(seconds=int(secs)))
    t = Table.grid(expand=True, padding=(0, 1))
    t.add_column(ratio=1)
    t.add_column(justify="right")
    # battery: invert color (high is good)
    inv_style = color_for_pct(100 - pct)
    t.add_row(
        bar(pct, 30),
        Text(f"{pct:.0f}%  {status}  ({remain})", style=inv_style),
    )
    return Panel(t, title="Battery", border_style="bright_yellow")


_proc_cache: dict = {"rows": [], "ts": 0.0}


def _proc_subtable(procs: list, key: str, n: int) -> Table:
    sorted_procs = sorted(procs, key=lambda x: (x.get(key) or 0), reverse=True)[:n]
    t = Table(expand=True, show_edge=False, pad_edge=False, box=None)
    t.add_column("PID", justify="right", style="grey62", width=6)
    t.add_column("CPU%" if key == "cpu_percent" else "MEM%",
                 justify="right", width=5,
                 style="bold" if True else "")
    t.add_column("Process", overflow="ellipsis")
    for info in sorted_procs:
        val = info.get(key) or 0.0
        t.add_row(
            str(info.get("pid", "")),
            Text(f"{val:4.1f}", style=color_for_pct(val)),
            Text(info.get("name") or "", style="white"),
        )
    return t


def top_proc_panel(n: int = 10, refresh: bool = True) -> Panel:
    if refresh or not _proc_cache["rows"]:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except Exception:
                continue
        _proc_cache["rows"] = procs
        _proc_cache["ts"] = time.time()
    procs = _proc_cache["rows"]
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        Panel(_proc_subtable(procs, "cpu_percent", n),
              title="by CPU", border_style="grey37", padding=(0, 1)),
        Panel(_proc_subtable(procs, "memory_percent", n),
              title="by MEM", border_style="grey37", padding=(0, 1)),
    )
    return Panel(grid, title="Top processes", border_style="grey50")


def build_layout() -> Layout:
    root = Layout()
    root.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    root["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="right", ratio=2),
    )
    root["left"].split_column(
        Layout(name="cpu", ratio=3),
        Layout(name="procs", ratio=4),
    )
    root["right"].split_column(
        Layout(name="mem", size=9),
        Layout(name="power", size=22),
        Layout(name="disk"),
        Layout(name="net", size=6),
        Layout(name="wifi", size=6),
        Layout(name="batt", size=5),
    )
    return root


def render(layout: Layout, pm: PowerMetricsReader, tick: int) -> None:
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    layout["header"].update(header_panel())
    layout["cpu"].update(cpu_panel(per_core))
    layout["procs"].update(top_proc_panel(12, refresh=(tick % PROC_EVERY == 0)))
    layout["mem"].update(mem_panel())
    layout["power"].update(power_panel(pm))
    layout["disk"].update(disk_panel(refresh_parts=(tick % DISK_EVERY == 0)))
    layout["net"].update(net_panel())
    layout["wifi"].update(wifi_panel())
    batt = battery_panel()
    if batt is None:
        layout["batt"].update(Panel(Align.center(Text("no battery", style="grey50")),
                                    title="Battery", border_style="grey30"))
    else:
        layout["batt"].update(batt)


def main() -> None:
    console = Console()
    if shutil.which("pmset") is None:
        console.print("[yellow]pmset not found — throttling info unavailable[/yellow]")
    psutil.cpu_percent(interval=None, percpu=True)  # prime
    pm = PowerMetricsReader(interval_ms=POWER_INTERVAL_MS)
    pm.start()
    layout = build_layout()
    tick = 0
    try:
        with Live(layout, console=console, refresh_per_second=2, screen=True):
            while True:
                render(layout, pm, tick)
                tick += 1
                time.sleep(REFRESH)
    except KeyboardInterrupt:
        pass
    finally:
        pm.stop()


if __name__ == "__main__":
    main()
