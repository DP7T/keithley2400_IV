#!/usr/bin/env python3
"""
Keithley 2400 I-V Sweep - desktop GUI

Hardware: Keithley 2400 via NI GPIB-USB-HS (default address GPIB0::24::INSTR)

Install (Windows):
  1. NI-488.2 driver (includes NI-VISA) from ni.com - needed for the GPIB-USB-HS
  2. pip install pyvisa numpy matplotlib
  3. On the 2400: MENU > COMMUNICATION > GPIB, address 24 (or set yours in the app)

Run:
  python keithley2400_gui.py
"""

import csv
import json
import datetime as dt
import math
import os
import queue
import random
import re
from decimal import Decimal
import sys
import threading
import time
import logging
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ----------------------------------------------------------------------------
# Error log + last-resort error pop-up (set up before the heavy imports, so a
# missing package at startup is also reported instead of failing silently)
# ----------------------------------------------------------------------------
def _log_path():
    if getattr(sys, "frozen", False):            # running as KeithleyIV.exe: log next to the exe
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, os.path.expanduser("~")):
        if os.access(folder, os.W_OK):
            return os.path.join(folder, "keithley_iv_errors.log")
    return os.path.join(os.path.expanduser("~"), "keithley_iv_errors.log")


LOG_FILE = _log_path()
log = logging.getLogger("keithley_iv")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_FILE, delay=True, encoding="utf-8")   # file created on first entry
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
log.addHandler(_fh)


def _popup_fatal(title, text):
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, text, parent=root)
        root.destroy()
    except Exception:
        pass


def _fatal_hook(exc_type, exc, tb):
    log.error("Unhandled error:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
    if issubclass(exc_type, ModuleNotFoundError):
        msg = (f"A required package is missing: {exc.name}\n\n"
               "Open PowerShell and run:\n"
               "py -m pip install --user pyvisa numpy matplotlib")
    else:
        msg = f"{exc_type.__name__}: {exc}"
    _popup_fatal("Keithley IV — error", f"{msg}\n\nDetails were saved to:\n{LOG_FILE}")


def hint_for(e):
    """Plain-language hint for common VISA / GPIB errors."""
    s = str(e)
    if "VI_ERROR_TMO" in s or "Timeout" in s:
        return ("\n\nHint: the 2400 did not answer. Check it is on, set to GPIB "
                "(MENU > COMMUNICATION), and that the address matches.")
    if "VI_ERROR_RSRC_NFOUND" in s or "not found" in s.lower():
        return ("\n\nHint: no instrument at that address. Click Refresh, and check the "
                "GPIB-USB-HS cable and the 2400's GPIB address.")
    if "VISA" in s and ("locate" in s or "library" in s.lower()):
        return "\n\nHint: NI-488.2 / NI-VISA is not installed on this PC."
    if "VI_ERROR_NLISTENERS" in s or "VI_ERROR_NCIC" in s:
        return "\n\nHint: GPIB bus problem. Check the cable and that the 2400 is powered on."
    if isinstance(e, PermissionError):
        return ("\n\nHint: the data file could not be written. If it is open in Excel, "
                "close it, or choose another folder under Saving in the Sweep tab.")
    if isinstance(e, OSError) and "VI_" not in s:
        return "\n\nHint: problem with the data folder — check it under Saving in the Sweep tab."
    return ""


sys.excepthook = _fatal_hook
threading.excepthook = lambda a: _fatal_hook(a.exc_type, a.exc_value, a.exc_traceback)

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk


# ============================================================================
# Instrument drivers
# ============================================================================
class Keithley2400:
    def __init__(self, resource, timeout_ms=10000):
        import pyvisa
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        if resource.upper().startswith("ASRL"):
            self.inst.baud_rate = 9600
            self.inst.write_termination = "\r"
            self.inst.read_termination = "\r"
        else:
            self.inst.write_termination = "\n"
            self.inst.read_termination = "\n"
            try:
                self.inst.clear()          # GPIB device clear
            except Exception:
                pass
        self.mode = "voltage"

    def write(self, cmd):
        self.inst.write(cmd)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def idn(self):
        return self.query("*IDN?")

    def check_errors(self, max_reads=20):
        """Read the 2400's error queue. Returns a list of error strings (empty = OK)."""
        errors = []
        for _ in range(max_reads):
            e = self.query(":SYST:ERR?")
            if e.startswith("0") or e.startswith("+0"):
                break
            errors.append(e)
        return errors

    def configure(self, mode, compliance, nplc, four_wire, rear, src_range=None, meas_range=None):
        """src_range / meas_range: None = auto range, otherwise a fixed range value."""
        self.write("*RST")
        self.write("*CLS")
        self.write(":SYST:BEEP:STAT OFF")
        self.write(f":ROUT:TERM {'REAR' if rear else 'FRON'}")
        self.write(f":SYST:RSEN {'ON' if four_wire else 'OFF'}")
        src, meas = ("VOLT", "CURR") if mode == "voltage" else ("CURR", "VOLT")
        self.write(f":SOUR:FUNC {src}")
        self.write(f":SOUR:{src}:MODE FIXED")
        if src_range:
            self.write(f":SOUR:{src}:RANG:AUTO OFF")
            self.write(f":SOUR:{src}:RANG {src_range:.6E}")
        else:
            self.write(f":SOUR:{src}:RANG:AUTO ON")
        self.write(f':SENS:FUNC "{meas}"')
        self.write(f":SENS:{meas}:PROT {compliance:.6E}")
        self.write(f":SENS:{meas}:NPLC {nplc}")
        if meas_range:
            self.write(f":SENS:{meas}:RANG:AUTO OFF")
            self.write(f":SENS:{meas}:RANG {meas_range:.6E}")
        else:
            self.write(f":SENS:{meas}:RANG:AUTO ON")
        self.write(":FORM:ELEM VOLT,CURR,STAT")
        self.mode = mode

    def set_source(self, value):
        self.write(f":SOUR:{'VOLT' if self.mode == 'voltage' else 'CURR'} {value:.6E}")

    def output(self, on):
        self.write(f":OUTP {'ON' if on else 'OFF'}")

    def measure(self):
        """Returns (V, I, in_compliance, over_range). An over-range reading is NaN."""
        v, i, stat = (float(x) for x in self.query(":READ?").split(","))
        stat = int(stat)
        over = bool(stat & 1)      # status bit 0 = overflow
        if abs(v) > 9e37:          # the 2400 returns +9.9E37 when the reading is above range
            v, over = float("nan"), True
        if abs(i) > 9e37:
            i, over = float("nan"), True
        # bit 3 = compliance, bit 16 = range compliance (limit set by a fixed measure range)
        hit = bool(stat & (1 << 3)) or bool(stat & (1 << 16))
        return v, i, hit, over

    def close(self):
        try:
            self.output(False)
            self.write(":SYST:LOC")
        except Exception:
            pass
        try:
            self.inst.close()
        except Exception:
            pass


class SimulatedSMU:
    """Diode model for trying the app without an instrument."""
    def __init__(self, *a, **k):
        self.mode, self.src, self.on, self.compliance = "voltage", 0.0, False, 0.1

    def idn(self):
        return "SIMULATED,MODEL 2400,0,0"

    def check_errors(self, max_reads=20):
        return []

    def configure(self, mode, compliance, nplc=1, four_wire=False, rear=False,
                  src_range=None, meas_range=None):
        self.mode, self.compliance, self.meas_range = mode, compliance, meas_range

    def set_source(self, value):
        self.src = value

    def output(self, on):
        self.on = on

    @staticmethod
    def _diode(v):
        return 1e-12 * (math.exp(min(v / (1.5 * 0.02585), 80)) - 1) + v / 1e6

    def measure(self):
        time.sleep(0.01)
        if not self.on:
            return 0.0, 0.0, False, False
        comp = min(self.compliance, self.meas_range) if self.meas_range else self.compliance
        if self.mode == "voltage":
            i = self._diode(self.src) * (1 + random.gauss(0, 0.01)) + random.gauss(0, 1e-11)
            hit = abs(i) >= comp
            return self.src, max(-comp, min(comp, i)), hit, False
        lo, hi = -comp, comp
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self._diode(mid) < self.src else (lo, mid)
        v = (lo + hi) / 2
        return v, self.src, abs(v) >= comp * 0.999, False

    def close(self):
        self.on = False


CYCLE_SHAPE = "Cycle (0>max>min>0)"
CONST_SHAPE = "Constant (vs time)"
SHAPES = ["Single", "Double (fwd + rev)", "Bipolar (0>+>->0)", CYCLE_SHAPE, CONST_SHAPE]
SCALES = ["Linear", "Log |y|", "Log-log"]
V_RANGES = ["Auto", "200 mV", "2 V", "20 V", "200 V"]
I_RANGES = ["Auto", "1 µA", "10 µA", "100 µA", "1 mA", "10 mA", "100 mA", "1 A"]
MAX_RUNS_SHOWN = 12

# a data row: index, time_s, source, V, I, compliance, cycle, R_fit, over_range
COLUMNS = ["index", "time_s", "source", "voltage_V", "current_A", "compliance", "cycle",
           "resistance_ohm", "over_range"]


def build_sweep(start, stop, step, shape, cycles=1):
    """Returns (source_points, cycle_number_per_point).
    For the Cycle shape, start = max limit and stop = min limit."""
    step = abs(step)

    def ramp(a, b):
        n = int(round(abs(b - a) / step))
        return np.linspace(a, b, n + 1) if n > 0 else np.array([a])

    if shape == CYCLE_SHAPE:
        vmax, vmin = start, stop
        one = np.concatenate([ramp(0, vmax), ramp(vmax, vmin)[1:], ramp(vmin, 0)[1:]])
        pts, cyc = [one], [np.ones(len(one), int)]
        for c in range(2, int(cycles) + 1):       # next cycles continue from the shared 0
            pts.append(one[1:])
            cyc.append(np.full(len(one) - 1, c))
        return np.concatenate(pts), np.concatenate(cyc)

    if shape == "Single":
        pts = ramp(start, stop)
    elif shape == "Double (fwd + rev)":
        f = ramp(start, stop)
        pts = np.concatenate([f, f[-2::-1]])
    else:                                          # Bipolar 0 > +max > -max > 0
        m = max(abs(start), abs(stop))
        pts = np.concatenate([ramp(0, m), ramp(m, -m)[1:], ramp(-m, 0)[1:]])
    return pts, np.ones(len(pts), int)


# ============================================================================
# Numbers, SI prefixes, durations
# ============================================================================
_SI_IN = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
          "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_NUM = r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
_NUM_RE = re.compile(r"^\s*" + _NUM + r"\s*([fpnuµμmkKMG]?)\s*(?:V|A|s|sec|Ω|ohm|Ohm)?\s*$")
_DUR_RE = re.compile(r"^\s*" + _NUM + r"\s*(ms|s|sec|min|h|hr|hrs|hours?)?\s*$", re.I)


def parse_num(text, name="Value"):
    """Accepts 0.1, 1e-1, 100m, 100 mV, 10u, 10µA, 2k … (m = milli, M = mega)."""
    m = _NUM_RE.match(str(text).replace(",", "."))
    if not m:
        raise ValueError(f"{name}: '{text}' is not a number.\n"
                         "Examples: 0.1   1e-1   100m   10u   2k")
    if not m.group(2):
        return float(m.group(1))
    exp = int(round(math.log10(_SI_IN[m.group(2)])))
    return float(Decimal(m.group(1)).scaleb(exp))     # exact: 10u -> 1e-05


def parse_duration(text, name="Duration"):
    """Seconds from '90', '90 s', '500 ms', '10 min', '1.5 h'."""
    m = _DUR_RE.match(str(text).replace(",", "."))
    if not m:
        raise ValueError(f"{name}: '{text}' is not a duration.\n"
                         "Examples: 90   90 s   500 ms   10 min   1.5 h\n"
                         "(write 'min' for minutes — a lone 'm' is ambiguous)")
    unit = (m.group(2) or "s").lower()
    mult = {"ms": 1e-3, "min": 60.0, "h": 3600.0, "hr": 3600.0, "hrs": 3600.0,
            "hour": 3600.0, "hours": 3600.0}.get(unit, 1.0)
    return float(m.group(1)) * mult


_SI_OUT = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}


def si_exp(x):
    if not x or not math.isfinite(x):
        return 0
    return max(-15, min(9, int(math.floor(math.log10(abs(x)) / 3) * 3)))


def fmt_si(x, unit, digits=4):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if math.isinf(x):
        return f"∞ {unit}"
    e = si_exp(x)
    return f"{x / 10 ** e:+.{digits}g} {_SI_OUT[e]}{unit}".lstrip("+")


def fmt_dur(sec):
    if sec < 120:
        return f"{sec:.3g} s"
    if sec < 7200:
        return f"{sec / 60:.3g} min"
    return f"{sec / 3600:.3g} h"


def fmt_clock(sec):
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ============================================================================
# File / folder naming
# ============================================================================
# (label shown in the app, pattern). Every measurement gets its own folder with this name,
# containing <name>.csv and <name>.png.
NAME_SCHEMES = [
    ("Sample_Date_#", "{sample}_{date}_{n}"),
    ("Sample_Type_Date_#", "{sample}_{type}_{date}_{n}"),
    ("Date_Sample_#", "{date}_{sample}_{n}"),
    ("Sample_Date_Time", "{sample}_{date}_{time}"),
    ("Sample_Type_Range_Date_#", "{sample}_{type}_{range}_{date}_{n}"),
    ("Sample_#", "{sample}_{n}"),
    ("Custom pattern", None),
]
NAME_TOKENS = ("sample", "date", "time", "n", "type", "range")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def safe_part(text):
    """Make text safe for a Windows file/folder name."""
    return _BAD_CHARS.sub("_", str(text)).strip("._") or "x"


def ascii_si(x, unit):
    """Compact ASCII value for names: 0.02 → 20mV, 1e-6 → 1uA."""
    return fmt_si(x, unit, 3).replace(" ", "").replace("µ", "u")


def name_tokens(cfg, when):
    u = "V" if cfg.get("mode", "voltage") == "voltage" else "A"
    sh = cfg.get("shape", "")
    if sh == CONST_SHAPE:
        typ = "IT" if u == "V" else "VT"
        rng = f"{ascii_si(cfg['level'], u)}_{fmt_dur(cfg['duration']).replace(' ', '')}" \
            if "level" in cfg else "range"
    else:
        typ = "IVcyc" if sh == CYCLE_SHAPE else ("VI" if u == "A" else "IV")
        if "start" in cfg:
            rng = f"{ascii_si(cfg['start'], u)}_{ascii_si(cfg['stop'], u)}"
            if sh == CYCLE_SHAPE:
                rng += f"_x{cfg['cycles']}"
        else:
            rng = "range"
    return dict(sample=safe_part(cfg.get("sample") or "sample"), date=when.strftime("%Y-%m-%d"),
                time=when.strftime("%H-%M-%S"), type=typ, range=rng)


def check_pattern(pattern):
    """Raises ValueError with a readable message if the pattern can't be used."""
    if not pattern or not pattern.strip():
        raise ValueError("The name pattern is empty.")
    try:
        pattern.format(**{k: "x" for k in NAME_TOKENS})
    except KeyError as e:
        raise ValueError(f"Unknown item {{{e.args[0]}}} in the name pattern.\n"
                         "Allowed: " + "  ".join("{%s}" % k for k in NAME_TOKENS))
    except (ValueError, IndexError):
        raise ValueError("The name pattern has unmatched { } brackets.")


def next_name(parent, pattern, tokens):
    """Next free name in `parent`. {n} counts up per name (001, 002, …), continuing after
    the highest number already in the folder."""
    check_pattern(pattern)
    existing = set(os.listdir(parent)) if os.path.isdir(parent) else set()
    if "{n}" in pattern:
        marker = "\x00N\x00"
        rendered = safe_part(pattern.format(**tokens, n=marker).replace(marker, "NUMBERHERE"))
        rx = re.compile("^" + re.escape(rendered).replace("NUMBERHERE", r"(\d+)") + "$")
        nums = [int(m.group(1)) for f in existing for m in [rx.match(f)] if m]
        return rendered.replace("NUMBERHERE", f"{(max(nums) + 1) if nums else 1:03d}")
    name = safe_part(pattern.format(**tokens))
    out, k = name, 2
    while out in existing:
        out, k = f"{name}_{k}", k + 1
    return out


def axis_scale(values, unit):
    """Scale factor + unit label so the largest |value| is between 1 and 1000."""
    vals = [abs(v) for v in values if v is not None and math.isfinite(v)]
    e = si_exp(max(vals)) if vals else 0
    return 10.0 ** -e, _SI_OUT[e] + unit


# ============================================================================
# Resistance
# ============================================================================
def valid_rows(rows):
    """Rows that are usable for fitting: not in compliance, not over range, finite."""
    return [r for r in rows if not r[5] and not r[8]
            and math.isfinite(r[3]) and math.isfinite(r[4])]


def fit_resistance(rows, src_v, kind):
    """Quick R from a few rows. Sweep: slope of a straight-line fit (dV/dI).
    Constant mode: mean V / mean I."""
    good = valid_rows(rows)
    if not good:
        return float("nan")
    V = np.array([r[3] for r in good])
    I = np.array([r[4] for r in good])
    try:
        if kind == "time" or len(good) < 2:
            mi = I.mean()
            return float(V.mean() / mi) if mi != 0 else float("inf")
        x, y = (V, I) if src_v else (I, V)
        if np.ptp(x) == 0:
            mi = I.mean()
            return float(V.mean() / mi) if mi != 0 else float("inf")
        k = np.polyfit(x, y, 1)[0]
        if src_v:
            return float(1.0 / k) if k != 0 else float("inf")
        return float(k)
    except Exception:
        return float("nan")


def fit_stats(rows, src_v, kind):
    """Fit over a whole run. Sweep: linear fit with R and R².
    Constant mode: mean and spread of V/I."""
    good = valid_rows(rows)
    if len(good) < 2:
        return None
    V = np.array([r[3] for r in good])
    I = np.array([r[4] for r in good])
    try:
        if kind == "time":
            m = I != 0
            if not m.any():
                return None
            R = V[m] / I[m]
            return dict(kind="time", R=float(R.mean()), std=float(R.std()), n=int(m.sum()))
        x, y = (V, I) if src_v else (I, V)
        if np.ptp(x) == 0:
            return None
        slope, icpt = np.polyfit(x, y, 1)
        ss_res = float(np.sum((y - (slope * x + icpt)) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
        R = (1.0 / slope if slope != 0 else float("inf")) if src_v else slope
        return dict(kind="iv", R=float(R), r2=r2, n=len(good), slope=float(slope),
                    icpt=float(icpt), src_v=src_v, vmin=float(V.min()), vmax=float(V.max()))
    except Exception:
        return None


# ============================================================================
# CSV
# ============================================================================
def csv_header(cfg, idn, started):
    c = cfg
    unit = "V" if c["mode"] == "voltage" else "A"
    lines = [f"Instrument: {idn}",
             f"Date: {started.isoformat(timespec='seconds')}",
             f"Sample: {c['sample']}"]
    if c["shape"] == CONST_SHAPE:
        lines.append(f"Source: {c['mode']}, shape=constant, level={c['level']} {unit}, "
                     f"duration={c['duration']} s (0 = until stopped), interval={c['delay']} s")
    elif c["shape"] == CYCLE_SHAPE:
        lines.append(f"Source: {c['mode']}, shape={c['shape']}, max={c['start']}, "
                     f"min={c['stop']}, step={c['step']} {unit}, cycles={c['cycles']}")
    else:
        lines.append(f"Source: {c['mode']}, shape={c['shape']}, start={c['start']}, "
                     f"stop={c['stop']}, step={c['step']} {unit}")
    rng = lambda r: "auto" if not r else f"{r:g}"
    lines.append(f"Compliance: {c['compliance']}, NPLC: {c['nplc']}, delay: {c['delay']} s, "
                 f"source range: {rng(c.get('src_range'))}, "
                 f"measure range: {rng(c.get('meas_range'))}, "
                 f"4-wire: {c['four_wire']}, terminals: {'rear' if c['rear'] else 'front'}")
    if c.get("queue_note"):
        lines.append(f"Queue: {c['queue_note']}")
    lines.append(f"source column is in {unit}; resistance_ohm = fit over the last "
                 f"{c['rfit']} points; compliance/over_range: 1 = yes; empty = no reading")
    return "".join(f"# {s}\n" for s in lines)


def csv_cells(row):
    out = []
    for x in row:
        if isinstance(x, float):
            out.append("" if not math.isfinite(x) else f"{x:.10g}")
        else:
            out.append(x)
    return out


class LiveCSV:
    """Writes each row to disk as it is measured, so a crash or power cut loses nothing."""
    def __init__(self, path, header):
        self.fh = open(path, "w", newline="", encoding="utf-8")
        self.fh.write(header)
        self.w = csv.writer(self.fh)
        self.w.writerow(COLUMNS)
        self.fh.flush()
        self.last_sync = time.time()

    def add(self, row):
        self.w.writerow(csv_cells(row))
        self.fh.flush()
        if time.time() - self.last_sync > 2:
            os.fsync(self.fh.fileno())
            self.last_sync = time.time()

    def close(self):
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
        except Exception:
            pass
        self.fh.close()


# ============================================================================
# GUI
# ============================================================================
THEMES = {
    "dark": dict(
        bg="#1e1f22", panel="#2b2d31", field="#383a40", fg="#e3e5e8", muted="#8e9297",
        border="#45474d", accent="#4c8bf5", accent_hover="#3b78e7", btn="#3a3c42",
        btn_hover="#46484f", ax_bg="#232428", grid="#4a4c52", fit="#e3e5e8", danger="#ff7b72",
        lines=["#5b9bff", "#ff6b5b", "#3ecf8e", "#c38bff", "#ffb443", "#3fd0e6", "#bbbbbb"]),
    "light": dict(
        bg="#f0f1f3", panel="#ffffff", field="#ffffff", fg="#1f2328", muted="#6b7280",
        border="#c9ccd1", accent="#2a6fdb", accent_hover="#1f5dc0", btn="#e4e6ea",
        btn_hover="#d6d9de", ax_bg="#ffffff", grid="#d0d3d8", fit="#1f2328", danger="#c62828",
        lines=["#2a6fdb", "#e0533d", "#2b9e6b", "#9b59b6", "#e69b00", "#17a2b8", "#555555"]),
}
PUB_COLORS = ["#000000", "#d62728", "#1f5bd6", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]
PLOT_STYLES = ["Line + markers", "Line", "Scatter"]
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".keithley_iv_settings.json")
APP_VERSION = "1.1"
FORM_KEYS = ["mode", "shape", "start", "stop", "step", "comp", "cycles", "nplc", "delay",
             "rfit", "srange", "mrange", "4w", "rear", "stopcomp", "sample"]


class ScrollFrame(ttk.Frame):
    """A frame whose content scrolls vertically, with a scrollbar only when needed."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._bar_shown = False
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bind("<Enter>", lambda e: self._wheel(True))
        self.bind("<Leave>", lambda e: self._wheel(False))

    def set_bg(self, color):
        self.canvas.configure(bg=color)

    def _needs_bar(self):
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1

    def _on_inner(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"),
                              width=self.inner.winfo_reqwidth())
        self._update_bar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update_bar()

    def _update_bar(self):
        need = self._needs_bar()
        if need and not self._bar_shown:
            self.vbar.pack(side="right", fill="y")
            self._bar_shown = True
        elif not need and self._bar_shown:
            self.vbar.pack_forget()
            self._bar_shown = False
            self.canvas.yview_moveto(0)

    def _wheel(self, on):
        if on:
            self.bind_all("<MouseWheel>", self._on_wheel)
            self.bind_all("<Button-4>", lambda e: self._scroll(-1))
            self.bind_all("<Button-5>", lambda e: self._scroll(1))
        else:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.unbind_all(ev)

    def _on_wheel(self, e):
        self._scroll(-1 if e.delta > 0 else 1)

    def _scroll(self, d):
        if self._needs_bar():
            self.canvas.yview_scroll(d * 2, "units")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Keithley 2400 I-V Sweep  v{APP_VERSION}")
        h = max(700, min(980, self.winfo_screenheight() - 100))
        self.geometry(f"1280x{h}")
        self.minsize(1040, 700)

        self._last_err = ("", 0.0)
        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.worker = None
        self.busy = False
        self.runs = []          # dicts: idx, kind, src_v, label, rows, line, cyc_lines, cfg, ...
        self.run_counter = 0
        self.seq = []           # queue steps
        self.seq_state = None   # set while a queue is running
        self.dirty = False
        self.toolbar = None
        self.fit_line = None

        self._build_vars()
        self._load_settings()
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._build_ui()
        self.apply_theme()
        self._on_mode_change()
        self._on_name_change()
        self._q_refresh()
        self.refresh_resources()
        self.after(50, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- unexpected errors
    def report_callback_exception(self, exc_type, exc, tb):
        """Any unexpected error inside the GUI: log it and show a pop-up
        (the same error is shown at most once every 10 s, so it can't flood the screen)."""
        log.error("Unexpected GUI error:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        msg = f"{exc_type.__name__}: {exc}"
        last, when = self._last_err
        if msg == last and time.time() - when < 10:
            return
        self._last_err = (msg, time.time())
        self.v_status.set(f"Unexpected error: {msg}")
        messagebox.showerror("Unexpected error",
                             f"{msg}\n\nThe app is still running. Details were saved to:\n{LOG_FILE}",
                             parent=self)

    # ---------------------------------------------------------------- vars / settings
    def _build_vars(self):
        S, B = tk.StringVar, tk.BooleanVar
        self.v_resource = S(value="GPIB0::24::INSTR")
        self.v_simulate = B(value=False)
        self.v_mode = S(value="Source V, measure I")
        self.v_shape = S(value="Single")
        self.v_start = S(value="-1")
        self.v_stop = S(value="1")
        self.v_step = S(value="20m")
        self.v_comp = S(value="10m")
        self.v_cycles = S(value="3")
        self.v_nplc = S(value="1")
        self.v_delay = S(value="50m")
        self.v_rfit = S(value="10")
        self.v_srange = S(value="Auto")
        self.v_mrange = S(value="Auto")
        self.v_4w = B(value=False)
        self.v_rear = B(value=False)
        self.v_stopcomp = B(value=False)
        self.v_sample = S(value="sample1")
        self.v_folder = S(value=os.path.join(os.path.expanduser("~"), "IV_data"))
        self.v_autosave = B(value=True)
        self.v_namescheme = S(value=NAME_SCHEMES[0][0])
        self.v_namepat = S(value="{sample}_{date}_{n}")
        self.v_nameprev = S(value="")
        self.v_scale = S(value="Linear")
        self.v_showfit = B(value=True)
        self.v_cyclecolor = B(value=True)
        self.v_dark = B(value=True)
        self.v_pstyle = S(value="Line")
        self.v_lw = S(value="2")
        self.v_ms = S(value="4")
        self.v_wait = S(value="10 min")
        self.v_qrepeat = S(value="1")
        self.v_status = S(value="Ready")
        self.v_readV = S(value="—")
        self.v_readI = S(value="—")
        self.v_readR = S(value="—")
        self.v_points = S(value="0 / 0")
        self.v_fit = S(value="")
        self._persist = {k: getattr(self, "v_" + k) for k in [
            "resource", "simulate", "mode", "shape", "start", "stop", "step", "comp", "cycles",
            "nplc", "delay", "rfit", "srange", "mrange", "4w", "rear", "stopcomp", "sample",
            "folder", "autosave", "scale", "showfit", "cyclecolor", "dark", "pstyle", "lw", "ms",
            "wait", "qrepeat", "namescheme", "namepat"]}

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for k, var in self._persist.items():
            if k in data:
                try:
                    var.set(data[k])
                except Exception:
                    pass
        if "scale" not in data and data.get("logy"):
            self.v_scale.set("Log |y|")
        if self.v_shape.get() not in SHAPES:
            self.v_shape.set("Single")
        if self.v_scale.get() not in SCALES:
            self.v_scale.set("Linear")
        steps = data.get("queue", [])
        if isinstance(steps, list):
            self.seq = [s for s in steps if isinstance(s, dict) and s.get("type") in ("run", "wait")
                        and ("cfg" in s or "seconds" in s)]

    def _save_settings(self):
        try:
            data = {k: v.get() for k, v in self._persist.items()}
            data["queue"] = self.seq
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except Exception:
            log.warning("Could not save settings:\n%s", traceback.format_exc())

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        left = ttk.Frame(self, padding=(8, 8, 8, 30))      # bottom pad = room for status bar
        left.pack(side="left", fill="y")
        right = ttk.Frame(self, padding=(0, 8, 8, 28))
        right.pack(side="left", fill="both", expand=True)
        self.locked = []        # widgets disabled while a run / queue is going

        # --- Connection (always visible)
        con = ttk.LabelFrame(left, text="Connection", padding=6)
        con.pack(fill="x", pady=(0, 6))
        self.cb_res = ttk.Combobox(con, textvariable=self.v_resource, width=28)
        self.cb_res.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(con, text="Refresh", command=self.refresh_resources).grid(
            row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(con, text="Test (*IDN?)", command=self.test_connection).grid(
            row=1, column=1, sticky="ew", pady=(4, 0), padx=(4, 0))
        ttk.Checkbutton(con, text="Simulate (no instrument)", variable=self.v_simulate).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        con.columnconfigure((0, 1), weight=1)

        # --- Tabs
        self.nb = ttk.Notebook(left)
        self.nb.pack(fill="both", expand=True)
        tab_sweep = ttk.Frame(self.nb, padding=(0, 6, 0, 0))
        tab_queue = ttk.Frame(self.nb, padding=(0, 6, 0, 0))
        self.nb.add(tab_sweep, text="  Sweep  ")
        self.nb.add(tab_queue, text="  Queue  ")
        self.tab_sweep = tab_sweep
        self.sweep_scroll = ScrollFrame(tab_sweep)     # scrolls only if the screen is too short
        self.sweep_scroll.pack(fill="both", expand=True)
        self._build_sweep_tab(self.sweep_scroll.inner)
        self._build_saving(self.sweep_scroll.inner)
        self._build_queue_tab(tab_queue)

        # --- Sample + run buttons (always visible)
        bot = ttk.Frame(left, padding=(0, 6, 0, 0))
        bot.pack(fill="x")
        sf = ttk.Frame(bot)
        sf.pack(fill="x", pady=(0, 6))
        ttk.Label(sf, text="Sample").pack(side="left")
        ttk.Entry(sf, textvariable=self.v_sample).pack(side="left", fill="x", expand=True,
                                                       padx=(6, 0))
        self.btn_start = ttk.Button(bot, text="▶  Start", style="Accent.TButton",
                                    command=self.start)
        self.btn_start.pack(fill="x", ipady=4)
        sr = ttk.Frame(bot)
        sr.pack(fill="x", pady=(4, 0))
        self.btn_stop = ttk.Button(sr, text="■  Stop (keep data)", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", fill="x", expand=True, ipady=2)
        self.btn_discard = ttk.Button(sr, text="✕  Stop & discard", style="Danger.TButton",
                                      command=self.stop_discard, state="disabled")
        self.btn_discard.pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=2)
        bb = ttk.Frame(bot)
        bb.pack(fill="x", pady=(8, 0))
        b1 = ttk.Button(bb, text="Save last as…", command=self.save_as)
        b1.pack(side="left", expand=True, fill="x")
        b2 = ttk.Button(bb, text="Clear plot", command=self.clear_plot)
        b2.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self.locked += [self.btn_start, b2]

        # --- Readout
        ro = ttk.Frame(right)
        ro.pack(fill="x")
        for col, (name, var) in enumerate([("Voltage", self.v_readV), ("Current", self.v_readI),
                                           ("R (last N points)", self.v_readR),
                                           ("Points", self.v_points)]):
            f = ttk.Frame(ro, padding=(0, 0, 28, 0))
            f.grid(row=0, column=col, sticky="w")
            ttk.Label(f, text=name, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(f, textvariable=var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(right, textvariable=self.v_fit, style="Fit.TLabel").pack(anchor="w", pady=(4, 0))
        self.pbar = ttk.Progressbar(right, mode="determinate")
        self.pbar.pack(fill="x", pady=(6, 6))

        # --- Plot style bar
        ps = ttk.Frame(right)
        ps.pack(fill="x", pady=(0, 4))
        ttk.Label(ps, text="Plot").pack(side="left")
        c = ttk.Combobox(ps, textvariable=self.v_pstyle, values=PLOT_STYLES, state="readonly", width=13)
        c.pack(side="left", padx=(4, 10))
        c.bind("<<ComboboxSelected>>", lambda e: self._redraw())
        ttk.Label(ps, text="Scale").pack(side="left")
        c2 = ttk.Combobox(ps, textvariable=self.v_scale, values=SCALES, state="readonly", width=8)
        c2.pack(side="left", padx=(4, 10))
        c2.bind("<<ComboboxSelected>>", lambda e: self._redraw())
        ttk.Label(ps, text="Width").pack(side="left")
        s1 = ttk.Spinbox(ps, from_=0.5, to=6, increment=0.5, textvariable=self.v_lw, width=4,
                         command=self._redraw)
        s1.pack(side="left", padx=(4, 10))
        ttk.Label(ps, text="Marker").pack(side="left")
        s2 = ttk.Spinbox(ps, from_=1, to=15, increment=1, textvariable=self.v_ms, width=4,
                         command=self._redraw)
        s2.pack(side="left", padx=(4, 10))
        for s in (s1, s2):
            s.bind("<Return>", lambda e: self._redraw())
            s.bind("<FocusOut>", lambda e: self._redraw())
        ttk.Checkbutton(ps, text="Fit line", variable=self.v_showfit,
                        command=self._redraw).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(ps, text="Color by cycle", variable=self.v_cyclecolor,
                        command=self._redraw).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(ps, text="Dark", variable=self.v_dark,
                        command=self.apply_theme).pack(side="right")

        # --- Plot
        self.plot_frame = ttk.Frame(right)
        self.plot_frame.pack(fill="both", expand=True)
        self.fig = Figure(figsize=(7, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # --- Status bar
        sb = ttk.Label(self, textvariable=self.v_status, style="Status.TLabel", anchor="w",
                       padding=(8, 3))
        sb.place(relx=0, rely=1, anchor="sw", relwidth=1)

    def _build_sweep_tab(self, tab):
        sw = ttk.LabelFrame(tab, text="Sweep", padding=6)
        sw.pack(fill="x", pady=(0, 6))
        ttk.Label(sw, text="Mode").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(sw, textvariable=self.v_mode, state="readonly", width=20,
                          values=["Source V, measure I", "Source I, measure V"])
        cb.grid(row=0, column=1, sticky="ew", pady=1)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        ttk.Label(sw, text="Shape").grid(row=1, column=0, sticky="w")
        cs = ttk.Combobox(sw, textvariable=self.v_shape, state="readonly", width=20, values=SHAPES)
        cs.grid(row=1, column=1, sticky="ew", pady=1)
        cs.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        self.locked += [cb, cs]
        self.lbl_start = ttk.Label(sw, text="Start")
        self.lbl_stop = ttk.Label(sw, text="Stop")
        self.lbl_step = ttk.Label(sw, text="Step")
        self.lbl_cycles = ttk.Label(sw, text="Cycles")
        self.lbl_comp = ttk.Label(sw, text="Compliance")
        self.entries = {}
        for r, (lbl, var) in enumerate([(self.lbl_start, self.v_start),
                                        (self.lbl_stop, self.v_stop),
                                        (self.lbl_step, self.v_step),
                                        (self.lbl_cycles, self.v_cycles),
                                        (self.lbl_comp, self.v_comp)], start=2):
            lbl.grid(row=r, column=0, sticky="w", padx=(0, 6))
            e = ttk.Entry(sw, textvariable=var, width=20)
            e.grid(row=r, column=1, sticky="ew", pady=1)
            self.entries[str(var)] = e
        self.lbl_hint = ttk.Label(sw, text="", style="Muted.TLabel", wraplength=250)
        self.lbl_hint.grid(row=7, column=0, columnspan=2, sticky="w")
        sw.columnconfigure(1, weight=1)

        me = ttk.LabelFrame(tab, text="Measurement", padding=6)
        me.pack(fill="x", pady=(0, 6))
        ttk.Label(me, text="NPLC (0.01–10)").grid(row=0, column=0, sticky="w")
        ttk.Entry(me, textvariable=self.v_nplc, width=12).grid(row=0, column=1, sticky="ew", pady=1)
        self.lbl_delay = ttk.Label(me, text="Delay per point (s)")
        self.lbl_delay.grid(row=1, column=0, sticky="w")
        ttk.Entry(me, textvariable=self.v_delay, width=12).grid(row=1, column=1, sticky="ew", pady=1)
        ttk.Label(me, text="R from last N points").grid(row=2, column=0, sticky="w")
        ttk.Entry(me, textvariable=self.v_rfit, width=12).grid(row=2, column=1, sticky="ew", pady=1)
        self.lbl_srange = ttk.Label(me, text="Source range")
        self.lbl_srange.grid(row=3, column=0, sticky="w")
        self.cb_srange = ttk.Combobox(me, textvariable=self.v_srange, state="readonly", width=10)
        self.cb_srange.grid(row=3, column=1, sticky="ew", pady=1)
        self.lbl_mrange = ttk.Label(me, text="Measure range")
        self.lbl_mrange.grid(row=4, column=0, sticky="w")
        self.cb_mrange = ttk.Combobox(me, textvariable=self.v_mrange, state="readonly", width=10)
        self.cb_mrange.grid(row=4, column=1, sticky="ew", pady=1)
        self.locked += [self.cb_srange, self.cb_mrange]
        ttk.Checkbutton(me, text="4-wire", variable=self.v_4w).grid(
            row=5, column=0, sticky="w", pady=(2, 0))
        ttk.Checkbutton(me, text="Rear terminals", variable=self.v_rear).grid(
            row=5, column=1, sticky="w", pady=(2, 0))
        ttk.Checkbutton(me, text="Stop at compliance", variable=self.v_stopcomp).grid(
            row=6, column=0, columnspan=2, sticky="w")
        me.columnconfigure(1, weight=1)

    def _build_queue_tab(self, tab):
        lf = ttk.LabelFrame(tab, text="Steps", padding=6)
        lf.pack(fill="both", expand=True, pady=(0, 6))
        box = ttk.Frame(lf)
        box.pack(fill="both", expand=True)
        self.q_list = tk.Listbox(box, height=10, width=36, activestyle="none", borderwidth=1,
                                 relief="flat", exportselection=False, font=("Segoe UI", 9))
        sbar = ttk.Scrollbar(box, orient="vertical", command=self.q_list.yview)
        self.q_list.configure(yscrollcommand=sbar.set)
        self.q_list.pack(side="left", fill="both", expand=True)
        sbar.pack(side="left", fill="y")
        self.q_list.bind("<Double-Button-1>", self._q_load)
        row = ttk.Frame(lf)
        row.pack(fill="x", pady=(4, 0))
        btns = [ttk.Button(row, text="Up", width=4, command=lambda: self._q_move(-1)),
                ttk.Button(row, text="Down", width=5, command=lambda: self._q_move(1)),
                ttk.Button(row, text="Remove", command=self._q_remove),
                ttk.Button(row, text="Clear", command=self._q_clear)]
        for k, b in enumerate(btns):
            b.pack(side="left", padx=(0 if k == 0 else 3, 0), fill="x", expand=k >= 2)
        self.locked += btns

        ad = ttk.LabelFrame(tab, text="Add step", padding=6)
        ad.pack(fill="x", pady=(0, 6))
        b_add = ttk.Button(ad, text="+ Add current Sweep-tab settings", command=self._q_add_run)
        b_add.pack(fill="x")
        wr = ttk.Frame(ad)
        wr.pack(fill="x", pady=(4, 0))
        ttk.Label(wr, text="Wait").pack(side="left")
        ttk.Entry(wr, textvariable=self.v_wait, width=9).pack(side="left", padx=(6, 4))
        b_wait = ttk.Button(wr, text="+ Add wait", command=self._q_add_wait)
        b_wait.pack(side="left", fill="x", expand=True)
        self.locked += [b_add, b_wait]

        rn = ttk.LabelFrame(tab, text="Run", padding=6)
        rn.pack(fill="x")
        rr = ttk.Frame(rn)
        rr.pack(fill="x")
        ttk.Label(rr, text="Repeat whole queue").pack(side="left")
        ttk.Entry(rr, textvariable=self.v_qrepeat, width=5).pack(side="left", padx=(6, 4))
        ttk.Label(rr, text="times").pack(side="left")
        self.btn_runq = ttk.Button(rn, text="▶  Run queue", style="Accent.TButton",
                                   command=self.run_queue)
        self.btn_runq.pack(fill="x", pady=(6, 0), ipady=3)
        self.locked.append(self.btn_runq)
        ttk.Label(rn, text="Each step keeps the settings it had when added. Double-click a step "
                           "to load it back into the Sweep tab. Queue results are always saved. "
                           "The output is OFF during waits.",
                  style="Muted.TLabel", wraplength=255).pack(anchor="w", pady=(6, 0))

    def _build_saving(self, tab):
        da = ttk.LabelFrame(tab, text="Saving", padding=6)
        da.pack(fill="x", pady=(0, 6))
        ttk.Label(da, text="Folder").grid(row=0, column=0, sticky="w")
        ff = ttk.Frame(da)
        ff.grid(row=0, column=1, sticky="ew", pady=1)
        ttk.Entry(ff, textvariable=self.v_folder, width=12).pack(side="left", fill="x", expand=True)
        ttk.Button(ff, text="…", width=3, command=self._browse).pack(side="left", padx=(2, 0))
        ttk.Button(ff, text="Open", width=5, command=self._open_folder).pack(side="left", padx=(2, 0))
        ttk.Label(da, text="Names").grid(row=1, column=0, sticky="w")
        cbn = ttk.Combobox(da, textvariable=self.v_namescheme, state="readonly", width=20,
                           values=[lbl for lbl, _ in NAME_SCHEMES])
        cbn.grid(row=1, column=1, sticky="ew", pady=1)
        cbn.bind("<<ComboboxSelected>>", lambda e: self._on_name_change())
        self.ent_namepat = ttk.Entry(da, textvariable=self.v_namepat)
        self.ent_namepat.grid(row=2, column=1, sticky="ew", pady=1)
        self.lbl_pat = ttk.Label(da, text="Pattern")
        self.lbl_pat.grid(row=2, column=0, sticky="w")
        self.lbl_tokens = ttk.Label(da, text="{sample} {date} {time} {n} {type} {range}",
                                    style="Muted.TLabel", wraplength=255)
        self.lbl_tokens.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(da, textvariable=self.v_nameprev, style="Muted.TLabel", wraplength=255).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Checkbutton(da, text="Auto-save (live CSV + PNG at the end)",
                        variable=self.v_autosave).grid(row=5, column=0, columnspan=2, sticky="w",
                                                       pady=(4, 0))
        da.columnconfigure(1, weight=1)
        self.locked += [cbn, self.ent_namepat]
        for v in (self.v_sample, self.v_folder, self.v_namepat, self.v_shape, self.v_mode,
                  self.v_start, self.v_stop, self.v_cycles):
            v.trace_add("write", lambda *a: self._schedule_preview())

    # ---------------------------------------------------------------- theme
    @property
    def T(self):
        return THEMES["dark" if self.v_dark.get() else "light"]

    def apply_theme(self):
        t = self.T
        s = self.style
        self.configure(bg=t["bg"])
        s.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"],
                    bordercolor=t["border"], lightcolor=t["bg"], darkcolor=t["bg"],
                    troughcolor=t["field"], selectbackground=t["accent"],
                    selectforeground="#ffffff", insertcolor=t["fg"], arrowcolor=t["fg"],
                    font=("Segoe UI", 9))
        s.configure("TLabelframe", background=t["bg"], bordercolor=t["border"])
        s.configure("TLabelframe.Label", background=t["bg"], foreground=t["muted"],
                    font=("Segoe UI", 9, "bold"))
        s.configure("TButton", background=t["btn"], foreground=t["fg"], bordercolor=t["border"],
                    focuscolor=t["btn"], padding=(8, 3))
        s.map("TButton", background=[("disabled", t["bg"]), ("pressed", t["btn_hover"]),
                                     ("active", t["btn_hover"])],
              foreground=[("disabled", t["muted"])])
        s.configure("Danger.TButton", background=t["btn"], foreground=t["danger"],
                    bordercolor=t["border"], focuscolor=t["btn"], padding=(8, 3))
        s.map("Danger.TButton", background=[("disabled", t["bg"]), ("pressed", t["btn_hover"]),
                                            ("active", t["btn_hover"])],
              foreground=[("disabled", t["muted"])])
        s.configure("Accent.TButton", background=t["accent"], foreground="#ffffff",
                    focuscolor=t["accent"], font=("Segoe UI", 10, "bold"))
        s.map("Accent.TButton", background=[("disabled", t["btn"]), ("pressed", t["accent_hover"]),
                                            ("active", t["accent_hover"])],
              foreground=[("disabled", t["muted"])])
        for w in ("TEntry", "TSpinbox"):
            s.configure(w, fieldbackground=t["field"], foreground=t["fg"],
                        background=t["btn"], bordercolor=t["border"])
            s.map(w, fieldbackground=[("disabled", t["bg"])], foreground=[("disabled", t["muted"])])
        s.configure("TCombobox", fieldbackground=t["field"], foreground=t["fg"],
                    background=t["btn"], bordercolor=t["border"])
        s.map("TCombobox", fieldbackground=[("readonly", t["field"]), ("disabled", t["bg"])],
              foreground=[("disabled", t["muted"]), ("readonly", t["fg"])],
              selectbackground=[("readonly", t["field"])],
              selectforeground=[("readonly", t["fg"])])
        s.configure("TCheckbutton", background=t["bg"], foreground=t["fg"],
                    indicatorbackground=t["field"], indicatorforeground=t["fg"],
                    focuscolor=t["bg"])
        s.map("TCheckbutton", background=[("active", t["bg"])],
              indicatorbackground=[("selected", t["accent"]), ("active", t["btn_hover"])],
              indicatorforeground=[("selected", "#ffffff")])
        s.configure("TNotebook", background=t["bg"], bordercolor=t["border"], tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=t["btn"], foreground=t["muted"],
                    bordercolor=t["border"], padding=(10, 4), font=("Segoe UI", 9, "bold"))
        s.map("TNotebook.Tab", background=[("selected", t["bg"]), ("active", t["btn_hover"])],
              foreground=[("selected", t["fg"])],
              lightcolor=[("selected", t["bg"])], bordercolor=[("selected", t["border"])])
        s.configure("Vertical.TScrollbar", background=t["btn"], troughcolor=t["field"],
                    bordercolor=t["border"], lightcolor=t["btn"], darkcolor=t["btn"],
                    arrowcolor=t["fg"], gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", t["btn_hover"])])
        s.configure("Horizontal.TProgressbar", background=t["accent"], troughcolor=t["field"],
                    bordercolor=t["border"], lightcolor=t["accent"], darkcolor=t["accent"])
        s.configure("Big.TLabel", font=("Consolas", 16, "bold"), foreground=t["fg"])
        s.configure("Muted.TLabel", foreground=t["muted"])
        s.configure("Fit.TLabel", foreground=t["fg"], font=("Segoe UI", 10))
        s.configure("Status.TLabel", background=t["panel"], foreground=t["muted"])
        self.sweep_scroll.set_bg(t["bg"])
        self.q_list.configure(bg=t["field"], fg=t["fg"], selectbackground=t["accent"],
                              selectforeground="#ffffff", highlightthickness=1,
                              highlightbackground=t["border"], highlightcolor=t["accent"],
                              disabledforeground=t["muted"])

        for pat, val in [("*TCombobox*Listbox.background", t["field"]),
                         ("*TCombobox*Listbox.foreground", t["fg"]),
                         ("*TCombobox*Listbox.selectBackground", t["accent"]),
                         ("*TCombobox*Listbox.selectForeground", "#ffffff"),
                         ("*Button.background", t["bg"]), ("*Button.foreground", t["fg"]),
                         ("*Button.activeBackground", t["btn_hover"]),
                         ("*Checkbutton.background", t["bg"]),
                         ("*Checkbutton.selectColor", t["btn_hover"]),
                         ("*Label.background", t["bg"]), ("*Label.foreground", t["fg"]),
                         ("*Frame.background", t["bg"])]:
            self.option_add(pat, val)
        self._rebuild_toolbar()
        self._redraw()

    def _rebuild_toolbar(self):
        if self.toolbar is not None:
            self.toolbar.destroy()
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.plot_frame, pack_toolbar=False)
        t = self.T

        def paint(w):
            for opt, val in (("background", t["bg"]), ("foreground", t["fg"]),
                             ("highlightbackground", t["bg"])):
                try:
                    w.configure(**{opt: val})
                except tk.TclError:
                    pass
            for ch in w.winfo_children():
                paint(ch)
        paint(self.toolbar)
        self.toolbar.update()
        self.toolbar.pack(side="bottom", fill="x", before=self.canvas.get_tk_widget())

    def _line_kwargs(self):
        def f(var, default, lo, hi):
            try:
                return min(hi, max(lo, float(var.get())))
            except ValueError:
                return default
        lw, ms = f(self.v_lw, 2, 0.1, 10), f(self.v_ms, 4, 0.5, 30)
        st = self.v_pstyle.get()
        if st == "Line":
            return dict(linestyle="-", marker="None", lw=lw, ms=ms)
        if st == "Scatter":
            return dict(linestyle="None", marker="o", lw=lw, ms=ms)
        return dict(linestyle="-", marker="o", lw=lw, ms=ms)

    # ---------------------------------------------------------------- data → plot
    def _scale_flags(self):
        s = self.v_scale.get()
        return s == "Log-log", s in ("Log |y|", "Log-log")

    @staticmethod
    def _axes_info(run):
        """(x quantity, x unit, y quantity, y unit) for a run."""
        if run["kind"] == "time":
            return ("Time", "s", "Current", "A") if run["src_v"] else ("Time", "s", "Voltage", "V")
        return ("Voltage", "V", "Current", "A")

    @staticmethod
    def _xy(run, rows):
        if run["kind"] == "time":
            return [r[1] for r in rows], [(r[4] if run["src_v"] else r[3]) for r in rows]
        return [r[3] for r in rows], [r[4] for r in rows]

    def _segments(self, run, by_cycle):
        """List of (cycle or None, x, y). Each cycle after the first starts from the last
        point of the previous one so the curve stays connected."""
        rows = run["rows"]
        if not (by_cycle and run["cfg"].get("cycles", 1) > 1):
            x, y = self._xy(run, rows)
            return [(None, x, y)]
        groups, prev = {}, None
        for row in rows:
            c = row[6]
            if c not in groups:
                groups[c] = [prev] if prev is not None else []
            groups[c].append(row)
            prev = row
        if not groups:                       # run just started, no points yet
            return [(1, [], [])]
        return [(c, *self._xy(run, g)) for c, g in groups.items()]

    @staticmethod
    def _cycle_colors(n, palette):
        if n <= len(palette):
            return palette[:n]
        cmap = matplotlib.colormaps["plasma"]
        return [cmap(0.1 + 0.8 * k / max(n - 1, 1)) for k in range(n)]

    @staticmethod
    def _transform(x, y, sx, sy, logx, logy):
        nan = float("nan")
        if logx:
            x = [abs(v) if (math.isfinite(v) and v != 0) else nan for v in x]
        else:
            x = [v * sx for v in x]
        if logy:
            y = [max(abs(v), 1e-15) if math.isfinite(v) else nan for v in y]
        else:
            y = [v * sy for v in y]
        return x, y

    @staticmethod
    def _scaled(segs, xunit, yunit, logx, logy):
        """Pick SI prefixes from the data range (e.g. 0.02 V → 20 mV). Log axes keep base units."""
        xs = [v for _, x, _ in segs for v in x]
        ys = [v for _, _, y in segs for v in y]
        sx, xu = (1.0, xunit) if (logx or xunit == "s") else axis_scale(xs, xunit)
        sy, yu = (1.0, yunit) if logy else axis_scale(ys, yunit)
        return sx, xu, sy, yu

    @staticmethod
    def _labels(xq, xu, yq, yu, run_kind, logx, logy):
        xl = f"|{xq}| ({xu})" if (logx and run_kind != "time") else f"{xq} ({xu})"
        yl = f"|{yq}| ({yu})" if logy else f"{yq} ({yu})"
        return xl, yl

    def _redraw(self):
        t = self.T
        logx, logy = self._scale_flags()
        by_cycle = self.v_cyclecolor.get()
        kw = self._line_kwargs()
        ax = self.ax
        last = self.runs[-1] if self.runs else None

        plan, all_segs = [], []          # (artist, x, y, color, label, alpha)
        for r in self.runs:
            alpha = 1.0 if r is last else 0.4
            segs = self._segments(r, by_cycle)
            all_segs += segs
            cyc_lines = r.setdefault("cyc_lines", {})
            if segs and segs[0][0] is not None:          # colored by cycle
                r["line"].set_visible(False)
                r["line"].set_label("_hidden")
                colors = self._cycle_colors(r["cfg"]["cycles"], t["lines"])
                for c, x, y in segs:
                    if c not in cyc_lines:
                        cyc_lines[c], = ax.plot([], [])
                    plan.append((cyc_lines[c], x, y, colors[(c - 1) % len(colors)],
                                 f"{r['label']} · cycle {c}", alpha))
            else:
                for cl in cyc_lines.values():
                    cl.set_visible(False)
                    cl.set_label("_hidden")
                _, x, y = segs[0]
                plan.append((r["line"], x, y, t["lines"][r["idx"] % len(t["lines"])],
                             r["label"], alpha))

        xq, xunit, yq, yunit = self._axes_info(last) if last else ("Voltage", "V", "Current", "A")
        sx, xu, sy, yu = self._scaled(all_segs, xunit, yunit, logx, logy)
        for art, x, y, color, label, alpha in plan:
            xx, yy = self._transform(x, y, sx, sy, logx, logy)
            art.set_data(xx, yy)
            art.set_visible(True)
            art.set_label(label)
            art.set(color=color, linestyle=kw["linestyle"], marker=kw["marker"],
                    linewidth=kw["lw"], markersize=kw["ms"], alpha=alpha)

        self._update_fit(last, sx, sy, logx or logy)

        # axes styling (screen only — the saved PNG is drawn separately)
        self.fig.set_facecolor(t["bg"])
        ax.set_facecolor(t["ax_bg"])
        ax.set_xscale("log" if logx else "linear")
        ax.set_yscale("log" if logy else "linear")
        xl, yl = self._labels(xq, xu, yq, yu, last["kind"] if last else "iv", logx, logy)
        ax.set_xlabel(xl, color=t["fg"], fontsize=11)
        ax.set_ylabel(yl, color=t["fg"], fontsize=11)
        ax.tick_params(colors=t["fg"], which="both")
        for sp in ax.spines.values():
            sp.set_color(t["border"])
        ax.grid(True, which="both", color=t["grid"], alpha=0.6, lw=0.6)
        if not logx:
            ax.ticklabel_format(axis="x", useOffset=False)
        if not logy:
            ax.ticklabel_format(axis="y", useOffset=False)
        handles = [h for h in ax.get_lines() if h.get_visible() and not h.get_label().startswith("_")]
        if handles:
            leg = ax.legend(handles=handles, loc="best", fontsize=8,
                            ncol=2 if len(handles) > 8 else 1,
                            facecolor=t["panel"], edgecolor=t["border"], labelcolor=t["fg"])
            leg.get_frame().set_alpha(0.9)
        elif ax.get_legend():
            ax.get_legend().remove()
        ax.relim(visible_only=True)
        ax.autoscale_view()
        try:
            self.fig.tight_layout()
        except Exception:
            pass
        self.canvas.draw_idle()

    def _update_fit(self, run, sx, sy, log_axes):
        """Whole-run fit: text under the readout + dashed line on the screen plot."""
        st = fit_stats(run["rows"], run["src_v"], run["kind"]) if run else None
        if st is None:
            self.v_fit.set("")
        elif st["kind"] == "time":
            rel = 100 * st["std"] / abs(st["R"]) if st["R"] else float("nan")
            self.v_fit.set(f"All points:  mean R = {fmt_si(st['R'], 'Ω')}   ·   "
                           f"σ = {fmt_si(st['std'], 'Ω', 3)} ({rel:.2g} %)   ·   n = {st['n']}")
        else:
            scope = "all cycles" if run["cfg"].get("cycles", 1) > 1 else "all points"
            self.v_fit.set(f"Linear fit ({scope}):  R = {fmt_si(st['R'], 'Ω')}   ·   "
                           f"R² = {st['r2']:.5f}   ·   n = {st['n']}")
        show = (st is not None and st["kind"] == "iv" and self.v_showfit.get() and not log_axes)
        if self.fit_line is None:
            self.fit_line, = self.ax.plot([], [], linestyle=(0, (5, 3)), linewidth=1.3,
                                          zorder=5)
        if not show:
            self.fit_line.set_visible(False)
            self.fit_line.set_label("_fit")
            return
        vx = np.linspace(st["vmin"], st["vmax"], 50)
        if st["src_v"]:
            iy = st["slope"] * vx + st["icpt"]
        else:
            iy = (vx - st["icpt"]) / st["slope"] if st["slope"] else np.full_like(vx, np.nan)
        self.fit_line.set_data(vx * sx, iy * sy)
        self.fit_line.set_color(self.T["fit"])
        self.fit_line.set_visible(True)
        self.fit_line.set_label(f"Fit: {fmt_si(st['R'], 'Ω', 4)}")

    def _clear_plot_now(self):
        self.runs.clear()
        self.ax.clear()
        self.fit_line = None
        self._redraw()
        for v in (self.v_readV, self.v_readI, self.v_readR):
            v.set("—")
        self.v_points.set("0 / 0")
        self.pbar["value"] = 0

    def clear_plot(self):
        if not self.busy:
            self._clear_plot_now()

    # ---------------------------------------------------------------- helpers
    def _src_is_v(self):
        return self.v_mode.get().startswith("Source V")

    def _on_mode_change(self):
        src_v = self._src_is_v()
        u, c = ("V", "A") if src_v else ("A", "V")
        shape = self.v_shape.get()
        cyc, const = shape == CYCLE_SHAPE, shape == CONST_SHAPE
        if const:
            self.lbl_start.config(text=f"Level ({u})")
            self.lbl_stop.config(text="Duration")
        elif cyc:
            self.lbl_start.config(text=f"Max limit ({u})")
            self.lbl_stop.config(text=f"Min limit ({u})")
        else:
            self.lbl_start.config(text=f"Start ({u})")
            self.lbl_stop.config(text=f"Stop ({u})")
        self.lbl_step.config(text=f"Step ({u})")
        self.lbl_comp.config(text=f"Compliance ({c})")
        if not self.busy:
            self.entries[str(self.v_step)].config(state="disabled" if const else "normal")
            self.entries[str(self.v_cycles)].config(state="normal" if cyc else "disabled")
        self.lbl_delay.config(text="Interval between points (s)" if const else "Delay per point (s)")
        hint = {CYCLE_SHAPE: "Each cycle: 0 → max → min → 0",
                CONST_SHAPE: "Duration e.g. 60, 10 min, 2 h (0 = until Stop)"}.get(shape, "")
        self.lbl_hint.config(text=hint)
        if hint:
            self.lbl_hint.grid()
        else:
            self.lbl_hint.grid_remove()
        srcs, meas = (V_RANGES, I_RANGES) if src_v else (I_RANGES, V_RANGES)
        self.cb_srange["values"] = srcs
        self.cb_mrange["values"] = meas
        if self.v_srange.get() not in srcs:
            self.v_srange.set("Auto")
        if self.v_mrange.get() not in meas:
            self.v_mrange.set("Auto")
        self.lbl_srange.config(text=f"Source range ({u})")
        self.lbl_mrange.config(text=f"Measure range ({c})")

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.v_folder.get() or os.getcwd())
        if d:
            self.v_folder.set(d)

    def _open_folder(self):
        folder = self.v_folder.get() or os.getcwd()
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)                     # Windows
        except AttributeError:
            self.v_status.set(f"Data folder: {folder}")
        except Exception as e:
            messagebox.showerror("Folder", str(e), parent=self)

    def refresh_resources(self):
        try:
            import pyvisa
            res = list(pyvisa.ResourceManager().list_resources())
            self.cb_res["values"] = res
            gpib = [r for r in res if r.upper().startswith("GPIB")]
            if gpib and self.v_resource.get() not in res:
                self.v_resource.set(gpib[0])
            self.v_status.set(f"Found {len(res)} VISA resource(s): {', '.join(res) or 'none'}")
        except Exception as e:
            self.v_status.set(f"VISA not available: {e}")

    def test_connection(self):
        res = self.v_resource.get()
        self.v_status.set(f"Querying {res} …")

        def job():
            try:
                smu = SimulatedSMU() if self.v_simulate.get() else Keithley2400(res, timeout_ms=3000)
                idn = smu.idn()
                try:
                    smu.inst.write(":SYST:LOC")
                    smu.inst.close()
                except Exception:
                    pass
                self.q.put(("status", f"Connected: {idn}"))
            except Exception as e:
                log.error("Connection test failed (%s):\n%s", res, traceback.format_exc())
                self.q.put(("error", f"Could not reach {res}:\n{e}{hint_for(e)}"))
        threading.Thread(target=job, daemon=True).start()

    # ---------------------------------------------------------------- settings → cfg
    def _read_cfg(self):
        src_v = self._src_is_v()
        shape = self.v_shape.get()
        rng = lambda s, name: None if s.strip().lower() == "auto" else parse_num(s, name)
        cfg = dict(
            resource=self.v_resource.get().strip(), simulate=self.v_simulate.get(),
            mode="voltage" if src_v else "current", shape=shape,
            compliance=parse_num(self.v_comp.get(), "Compliance"),
            nplc=parse_num(self.v_nplc.get(), "NPLC"),
            delay=parse_num(self.v_delay.get(), "Delay"),
            src_range=rng(self.v_srange.get(), "Source range"),
            meas_range=rng(self.v_mrange.get(), "Measure range"),
            four_wire=self.v_4w.get(), rear=self.v_rear.get(),
            stop_on_compliance=self.v_stopcomp.get(),
            sample=self.v_sample.get().strip() or "sample", cycles=1,
        )
        n_fit = parse_num(self.v_rfit.get(), "R fit points")
        if n_fit < 2 or n_fit != int(n_fit):
            raise ValueError("R fit points must be a whole number, at least 2")
        cfg["rfit"] = int(n_fit)
        smax, cmax = (210.0, 1.05) if src_v else (1.05, 210.0)
        unit = "V" if src_v else "A"

        if shape == CONST_SHAPE:
            cfg["level"] = parse_num(self.v_start.get(), "Level")
            cfg["duration"] = parse_duration(self.v_stop.get(), "Duration")
            if cfg["duration"] < 0:
                raise ValueError("Duration cannot be negative (use 0 to run until Stop)")
            src_max = abs(cfg["level"])
        else:
            cfg["start"] = parse_num(self.v_start.get(), "Start / Max")
            cfg["stop"] = parse_num(self.v_stop.get(), "Stop / Min")
            cfg["step"] = parse_num(self.v_step.get(), "Step")
            if cfg["step"] == 0:
                raise ValueError("Step must be non-zero")
            if shape == CYCLE_SHAPE:
                cyc = parse_num(self.v_cycles.get(), "Cycles")
                if cyc < 1 or cyc != int(cyc):
                    raise ValueError("Cycles must be a whole number, at least 1")
                cfg["cycles"] = int(cyc)
                if cfg["start"] <= cfg["stop"]:
                    raise ValueError("Max limit must be greater than Min limit")
            src_max = max(abs(cfg["start"]), abs(cfg["stop"]))
        if src_max > smax:
            raise ValueError(f"Source value exceeds the 2400 limit ({smax:g} {unit})")
        if cfg["src_range"] and src_max > 1.05 * cfg["src_range"]:
            raise ValueError(f"The source goes up to {fmt_si(src_max, unit)}, but the fixed source "
                             f"range is {fmt_si(cfg['src_range'], unit)}.\n"
                             "Choose a larger source range, or Auto.")
        if not 0 < cfg["compliance"] <= cmax:
            raise ValueError(f"Compliance must be > 0 and ≤ {cmax:g}")
        if not 0.01 <= cfg["nplc"] <= 10:
            raise ValueError("NPLC must be between 0.01 and 10")
        if cfg["delay"] < 0:
            raise ValueError("Delay cannot be negative")
        return cfg

    def _cfg_warnings(self, cfg):
        out = []
        src_v = cfg["mode"] == "voltage"
        if cfg["shape"] != CONST_SHAPE:
            n = len(build_sweep(cfg["start"], cfg["stop"], cfg["step"], cfg["shape"], cfg["cycles"])[0])
            if n > 5000:
                out.append(("Many points", f"This sweep has {n} points."))
            src_max = max(abs(cfg["start"]), abs(cfg["stop"]))
        else:
            src_max = abs(cfg["level"])
        if src_v and src_max > 20:
            out.append(("High voltage", f"The source goes up to {src_max:g} V."))
        mr = cfg["meas_range"]
        if mr and cfg["compliance"] > 1.05 * mr:
            mu = "A" if src_v else "V"
            out.append(("Measure range", f"Compliance ({fmt_si(cfg['compliance'], mu)}) is above the "
                                         f"fixed measure range ({fmt_si(mr, mu)}).\nThe 2400 will "
                                         f"limit at {fmt_si(mr, mu)} instead (range compliance)."))
        return out

    def _cfg_from_form(self):
        """Validated cfg from the Sweep tab, or None (error shown / user cancelled)."""
        try:
            cfg = self._read_cfg()
        except ValueError as e:
            messagebox.showerror("Invalid setting", str(e), parent=self)
            return None
        for title, text in self._cfg_warnings(cfg):
            if not messagebox.askyesno(title, text + "\n\nContinue?", parent=self):
                return None
        return cfg

    # ---------------------------------------------------------------- run control
    def _set_running(self, running):
        for w in self.locked:
            try:
                w.configure(state="disabled" if running else
                            ("readonly" if isinstance(w, ttk.Combobox) else "normal"))
            except tk.TclError:
                pass
        self.btn_stop.config(state="normal" if running else "disabled")
        self.btn_discard.config(state="normal" if running else "disabled")
        if not running:
            self._on_mode_change()

    def start(self):
        if self.busy:
            return
        cfg = self._cfg_from_form()
        if cfg is None:
            return
        self._save_settings()
        if self._launch(cfg, save=self.v_autosave.get()):
            self._set_running(True)

    def stop(self):
        self.stop_evt.set()
        self.v_status.set("Stopping…")

    def stop_discard(self):
        """Stop now and delete the files of the measurement that is running."""
        if not self.busy:
            return
        running = self.worker is not None and self.worker.is_alive()
        if not running:                      # a queue wait step: nothing to discard
            self.stop()
            return
        run = self.runs[-1] if self.runs else None
        msg = "Stop now and delete the data of the current measurement?"
        if self.seq_state is not None:
            msg += "\n\nThe queue stops too. Measurements already finished are kept."
        if not messagebox.askyesno("Stop & discard", msg, icon="warning", parent=self):
            return
        if run is None:
            return
        if self.worker is not None and self.worker.is_alive() and self.runs and self.runs[-1] is run:
            run["discard"] = True            # handled when the run has stopped
            self.stop()
            self.v_status.set("Stopping and discarding…")
        else:                                # it finished while the dialog was open
            self._discard_run(run)
            if self.seq_state is not None:
                self.stop()

    def _discard_run(self, run):
        """Delete the files this run wrote and remove it from the plot."""
        removed = False
        if run.get("base"):
            for ext in (".csv", ".png"):
                try:
                    os.remove(run["base"] + ext)
                    removed = True
                except FileNotFoundError:
                    pass
                except Exception:
                    log.error("Could not delete %s:\n%s", run["base"] + ext, traceback.format_exc())
            try:
                os.rmdir(run["dir"])         # only removes the folder if it is now empty
            except OSError:
                pass
        if run in self.runs:
            self.runs.remove(run)
            for art in [run["line"], *run.get("cyc_lines", {}).values()]:
                try:
                    art.remove()
                except Exception:
                    pass
        for v in (self.v_readV, self.v_readI, self.v_readR):
            v.set("—")
        self.v_points.set("0 / 0")
        self.pbar["value"] = 0
        self._redraw()
        self.v_status.set("Measurement discarded — its data were deleted" if removed
                          else "Measurement discarded")
        self._update_preview()

    # ---------------------------------------------------------------- names / folders
    def _pattern(self):
        lbl = self.v_namescheme.get()
        for l, pat in NAME_SCHEMES:
            if l == lbl:
                return pat if pat is not None else self.v_namepat.get()
        return NAME_SCHEMES[0][1]

    def _on_name_change(self):
        custom = self._pattern_is_custom()
        if not self.busy:
            self.ent_namepat.config(state="normal" if custom else "disabled")
        if custom:
            self.lbl_pat.grid()
            self.ent_namepat.grid()
            self.lbl_tokens.grid()
        else:
            self.lbl_pat.grid_remove()
            self.ent_namepat.grid_remove()
            self.lbl_tokens.grid_remove()
        self._update_preview()

    def _pattern_is_custom(self):
        return dict(NAME_SCHEMES).get(self.v_namescheme.get(), "") is None

    def _schedule_preview(self):
        if getattr(self, "_prev_job", None):
            self.after_cancel(self._prev_job)
        self._prev_job = self.after(250, self._update_preview)

    def _update_preview(self):
        self._prev_job = None
        try:
            cfg = self._read_cfg()
        except Exception:
            cfg = dict(sample=self.v_sample.get(), shape=self.v_shape.get(),
                       mode="voltage" if self._src_is_v() else "current")
        try:
            parent = self.v_folder.get() or os.getcwd()
            name = next_name(parent, self._pattern(), name_tokens(cfg, dt.datetime.now()))
            self.v_nameprev.set(f"Next folder: {name}")
        except ValueError as e:
            self.v_nameprev.set(f"⚠ {str(e).splitlines()[0]}")
        except Exception:
            self.v_nameprev.set("")

    def _new_base(self, cfg, parent=None):
        """Create <parent>/<name>/ and return (folder, folder/<name>) for the files."""
        parent = parent or self.v_folder.get() or os.getcwd()
        os.makedirs(parent, exist_ok=True)
        name = next_name(parent, self._pattern(), name_tokens(cfg, dt.datetime.now()))
        folder = os.path.join(parent, name)
        os.makedirs(folder)
        return folder, os.path.join(folder, name)

    def _launch(self, cfg, save, parent=None, note=""):
        """Start one measurement (sweep or constant). Returns False if it could not start."""
        const = cfg["shape"] == CONST_SHAPE
        kind = "time" if const else "iv"
        base = folder = None
        if save:
            try:
                folder, base = self._new_base(cfg, parent)
            except ValueError as e:
                messagebox.showerror("File names", str(e), parent=self)
                return False
            except Exception as e:
                log.error("Could not prepare data folder:\n%s", traceback.format_exc())
                messagebox.showerror("Data folder", f"Could not use the data folder:\n{e}", parent=self)
                return False
        if const:
            sweep = cyc = None
        else:
            sweep, cyc = build_sweep(cfg["start"], cfg["stop"], cfg["step"], cfg["shape"],
                                     cfg["cycles"])

        if self.runs and self.runs[-1]["kind"] != kind:
            self._clear_plot_now()                   # don't mix I-V and time plots
        while len(self.runs) >= MAX_RUNS_SHOWN:      # keep the plot readable (data are on disk)
            old = self.runs.pop(0)
            for art in [old["line"], *old.get("cyc_lines", {}).values()]:
                art.remove()
        self.run_counter += 1
        idx = self.run_counter - 1
        label = f"{cfg['sample']} #{self.run_counter}"
        line, = self.ax.plot([], [], label=label)
        started = dt.datetime.now()
        self.runs.append(dict(idx=idx, kind=kind, src_v=cfg["mode"] == "voltage", label=label,
                              rows=[], line=line, cfg=cfg, idn="", started=started, base=base,
                              dir=folder))

        self.pbar.stop()
        if const and cfg["duration"] > 0:
            self.pbar.config(mode="determinate", maximum=cfg["duration"], value=0)
        elif const:
            self.pbar.config(mode="indeterminate")
            self.pbar.start(15)
        else:
            self.pbar.config(mode="determinate", maximum=len(sweep), value=0)
        self.v_points.set("0" if const else f"0 / {len(sweep)}")
        self.v_readR.set("—")
        self.busy = True
        self.run_note = note
        self.v_status.set((note + " — " if note else "") + "Running…")
        self._redraw()
        self.stop_evt.clear()
        path = base + ".csv" if base else None
        target, args = ((self._worker_const, (cfg, path, started)) if const else
                        (self._worker, (cfg, sweep, cyc, path, started)))
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()
        return True

    # ---------------------------------------------------------------- worker thread
    def _open_and_configure(self, cfg):
        smu = SimulatedSMU() if cfg["simulate"] else Keithley2400(cfg["resource"])
        idn = smu.idn()
        self.q.put(("idn", idn))
        smu.configure(cfg["mode"], cfg["compliance"], cfg["nplc"], cfg["four_wire"], cfg["rear"],
                      src_range=cfg.get("src_range"), meas_range=cfg.get("meas_range"))
        errs = smu.check_errors()
        if errs:                 # stop before the output is ever switched on
            smu.close()
            raise RuntimeError("The 2400 rejected the setup — measurement not started.\n\n"
                               "Instrument errors:\n" + "\n".join(errs))
        return smu, idn

    def _finish(self, smu, cfg, exc, live):
        q = self.q
        stopped = self.stop_evt.is_set()
        if smu is not None and exc is None:
            try:
                errs = smu.check_errors()
                if errs:
                    log.warning("Instrument errors during run: %s", errs)
                    q.put(("warn", "The 2400 reported errors during the measurement. "
                                   "Check the data before using it.\n\n" + "\n".join(errs)))
            except Exception:
                pass
        if exc is not None:
            log.error("Measurement error (resource=%s):\n%s", cfg.get("resource"),
                      "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            q.put(("error", f"Measurement error:\n{exc}{hint_for(exc)}"))
        if smu is not None:                          # output off first, then close the file
            try:
                smu.set_source(0.0)
                smu.output(False)
            except Exception:
                log.error("Could not switch output off:\n%s", traceback.format_exc())
                q.put(("error", "WARNING: could not confirm the output is OFF.\n"
                                "Check the 2400 front panel and press OUTPUT to turn it off."))
            smu.close()
        if live is not None:
            live.close()
        q.put(("done", dict(ok=exc is None, stopped=stopped and exc is None)))

    def _record(self, rows, live, cfg, kind, k, t, src, v, i, hit, cyc, over):
        """Store a point: compute R over the last N points, write it to disk, send it to the GUI."""
        rows.append((k, t, src, v, i, int(hit), cyc, float("nan"), int(over)))
        R = fit_resistance(rows[-cfg["rfit"]:], cfg["mode"] == "voltage", kind)
        row = rows[-1][:7] + (R, int(over))
        rows[-1] = row
        if live is not None:
            live.add(row)
        self.q.put(("point", row))

    def _worker(self, cfg, sweep, cyc, path, started):
        q, smu, err, live, rows = self.q, None, None, None, []
        try:
            smu, idn = self._open_and_configure(cfg)
            if path:
                live = LiveCSV(path, csv_header(cfg, idn, started))
            t0 = time.time()
            smu.set_source(float(sweep[0]))
            smu.output(True)
            for k, s in enumerate(sweep):
                if self.stop_evt.is_set():
                    q.put(("status", "Stopped by user"))
                    break
                smu.set_source(float(s))
                if cfg["delay"] > 0 and self.stop_evt.wait(cfg["delay"]):
                    q.put(("status", "Stopped by user"))
                    break
                v, i, hit, over = smu.measure()
                self._record(rows, live, cfg, "iv", k, time.time() - t0, float(s), v, i, hit,
                             int(cyc[k]), over)
                if hit and cfg["stop_on_compliance"]:
                    q.put(("status", "Compliance reached — sweep stopped"))
                    break
            else:
                q.put(("status", "Sweep complete"))
        except Exception as e:
            err = e
        finally:
            self._finish(smu, cfg, err, live)

    def _worker_const(self, cfg, path, started):
        q, smu, err, live, rows = self.q, None, None, None, []
        try:
            smu, idn = self._open_and_configure(cfg)
            if path:
                live = LiveCSV(path, csv_header(cfg, idn, started))
            smu.set_source(cfg["level"])
            smu.output(True)
            t0, k = time.time(), 0
            while True:
                if self.stop_evt.is_set():
                    q.put(("status", "Stopped by user"))
                    break
                if cfg["duration"] > 0 and time.time() - t0 >= cfg["duration"]:
                    q.put(("status", "Constant-source run complete"))
                    break
                v, i, hit, over = smu.measure()
                self._record(rows, live, cfg, "time", k, time.time() - t0, cfg["level"], v, i,
                             hit, 1, over)
                k += 1
                if hit and cfg["stop_on_compliance"]:
                    q.put(("status", "Compliance reached — run stopped"))
                    break
                if cfg["delay"] > 0 and self.stop_evt.wait(cfg["delay"]):
                    q.put(("status", "Stopped by user"))
                    break
        except Exception as e:
            err = e
        finally:
            self._finish(smu, cfg, err, live)

    # ---------------------------------------------------------------- queue (sequence)
    def _describe(self, cfg):
        u = "V" if cfg["mode"] == "voltage" else "A"
        f = lambda x: fmt_si(x, u, 3)
        sh = cfg["shape"]
        if sh == CONST_SHAPE:
            d = f"Hold {f(cfg['level'])} for {fmt_dur(cfg['duration'])}"
        elif sh == CYCLE_SHAPE:
            d = f"Cycle 0→{f(cfg['start'])}→{f(cfg['stop'])}→0, {f(cfg['step'])} ×{cfg['cycles']}"
        elif sh == "Single":
            d = f"Sweep {f(cfg['start'])}→{f(cfg['stop'])}, {f(cfg['step'])}"
        elif sh.startswith("Double"):
            d = f"Sweep {f(cfg['start'])}→{f(cfg['stop'])}→back, {f(cfg['step'])}"
        else:
            m = max(abs(cfg["start"]), abs(cfg["stop"]))
            d = f"Bipolar ±{f(m)}, {f(cfg['step'])}"
        return f"{d}  [{cfg['sample']}]"

    def _q_refresh(self, select=None):
        self.q_list.delete(0, "end")
        for n, s in enumerate(self.seq, 1):
            self.q_list.insert("end", f"{n:>2}. {s['desc']}")
        if select is not None and 0 <= select < len(self.seq):
            self.q_list.selection_set(select)
            self.q_list.see(select)

    def _q_sel(self):
        sel = self.q_list.curselection()
        return sel[0] if sel else None

    def _q_insert(self, step):
        i = self._q_sel()
        pos = len(self.seq) if i is None else i + 1
        self.seq.insert(pos, step)
        self._q_refresh(select=pos)
        self._save_settings()

    def _q_add_run(self):
        cfg = self._cfg_from_form()
        if cfg is None:
            return
        if cfg["shape"] == CONST_SHAPE and cfg["duration"] == 0:
            messagebox.showerror("Queue", "A constant-source step in a queue needs a duration "
                                          "(0 = until Stop would never finish).", parent=self)
            return
        form = {k: self._persist[k].get() for k in FORM_KEYS}
        self._q_insert(dict(type="run", cfg=cfg, form=form, desc=self._describe(cfg)))
        self.v_status.set("Step added to the queue")

    def _q_add_wait(self):
        try:
            sec = parse_duration(self.v_wait.get(), "Wait")
        except ValueError as e:
            messagebox.showerror("Invalid wait", str(e), parent=self)
            return
        if sec <= 0:
            messagebox.showerror("Invalid wait", "Wait must be longer than 0 s.", parent=self)
            return
        self._q_insert(dict(type="wait", seconds=sec, desc=f"Wait {fmt_dur(sec)} (output off)"))

    def _q_move(self, d):
        i = self._q_sel()
        if i is None or not 0 <= i + d < len(self.seq):
            return
        self.seq[i], self.seq[i + d] = self.seq[i + d], self.seq[i]
        self._q_refresh(select=i + d)
        self._save_settings()

    def _q_remove(self):
        i = self._q_sel()
        if i is None:
            return
        del self.seq[i]
        self._q_refresh(select=min(i, len(self.seq) - 1))
        self._save_settings()

    def _q_clear(self):
        if self.seq and messagebox.askyesno("Clear queue", "Remove all steps?", parent=self):
            self.seq.clear()
            self._q_refresh()
            self._save_settings()

    def _q_load(self, _event=None):
        if self.busy:
            return
        i = self._q_sel()
        if i is None or self.seq[i]["type"] != "run":
            return
        form = self.seq[i].get("form", {})
        self.v_mode.set(form.get("mode", self.v_mode.get()))
        for k, val in form.items():
            if k in self._persist:
                self._persist[k].set(val)
        self._on_mode_change()
        self.nb.select(self.tab_sweep)
        self.v_status.set(f"Loaded step {i + 1} into the Sweep tab — edit it, then "
                          "'Add current Sweep-tab settings' to add a new step")

    def run_queue(self):
        if self.busy:
            return
        if not any(s["type"] == "run" for s in self.seq):
            messagebox.showinfo("Queue", "The queue has no measurement steps yet.\n\n"
                                         "Set up a sweep in the Sweep tab, then click "
                                         "'Add current Sweep-tab settings'.", parent=self)
            return
        try:
            reps = parse_num(self.v_qrepeat.get(), "Repeat")
            if reps < 1 or reps != int(reps):
                raise ValueError("Repeat must be a whole number, at least 1")
        except ValueError as e:
            messagebox.showerror("Invalid setting", str(e), parent=self)
            return
        self._save_settings()
        base_dir = self.v_folder.get() or os.getcwd()
        qdir = os.path.join(base_dir, "Queue_" + dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        try:
            os.makedirs(qdir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Data folder", f"Could not create the queue folder:\n{e}", parent=self)
            return
        self.seq_state = dict(pos=0, round=1, rounds=int(reps), runs=0, warnings=[],
                              folder=qdir)
        self._set_running(True)
        self._seq_next()

    def _seq_next(self):
        st = self.seq_state
        if st is None:
            return
        if st["pos"] >= len(self.seq):
            st["round"] += 1
            st["pos"] = 0
            if st["round"] > st["rounds"]:
                self._seq_end("Queue finished")
                return
        step = self.seq[st["pos"]]
        self.q_list.selection_clear(0, "end")
        self.q_list.selection_set(st["pos"])
        self.q_list.see(st["pos"])
        note = f"Queue step {st['pos'] + 1}/{len(self.seq)}" + \
               (f", round {st['round']}/{st['rounds']}" if st["rounds"] > 1 else "")
        if step["type"] == "wait":
            self._seq_wait(step["seconds"], note)
            return
        cfg = dict(step["cfg"])
        cfg["resource"] = self.v_resource.get().strip()      # use the current connection
        cfg["simulate"] = self.v_simulate.get()
        cfg["queue_note"] = note
        if not self._launch(cfg, save=True, parent=st["folder"], note=note):
            self._seq_end("Queue stopped — could not start a step")

    def _seq_after_run(self, outcome):
        st = self.seq_state
        if outcome.get("stopped"):
            self._seq_end("Queue stopped — last measurement discarded" if st.get("discarded")
                          else "Queue stopped by user")
        elif not outcome.get("ok"):
            self._seq_end("Queue stopped because of an error")
        else:
            st["runs"] += 1
            st["pos"] += 1
            self.after(300, self._seq_next)

    def _seq_wait(self, seconds, note):
        st = self.seq_state
        self.busy = True
        self.stop_evt.clear()
        end = time.time() + seconds
        self.pbar.stop()
        self.pbar.config(mode="determinate", maximum=seconds, value=0)

        def tick():
            if self.seq_state is not st:
                return
            if self.stop_evt.is_set():
                self.busy = False
                self._seq_end("Queue stopped by user")
                return
            left = end - time.time()
            if left <= 0:
                self.busy = False
                st["pos"] += 1
                self._seq_next()
                return
            self.pbar["value"] = seconds - left
            self.v_points.set(f"wait {fmt_clock(left)}")
            self.v_status.set(f"{note} — waiting, {fmt_clock(left)} left (output off)")
            self.after(250, tick)
        tick()

    def _seq_end(self, msg):
        st, self.seq_state = self.seq_state, None
        self.busy = False
        self.pbar.stop()
        self._set_running(False)
        self.q_list.selection_clear(0, "end")
        if st:
            try:
                os.rmdir(st["folder"])               # remove the queue folder if nothing was saved
                msg += " — nothing was saved"
            except OSError:
                msg += f" — {st['runs']} run(s) saved in {st['folder']}"
        self.v_status.set(msg)
        self._update_preview()
        self.bell()
        if st and st["warnings"]:
            messagebox.showwarning("Queue warnings", "\n\n".join(st["warnings"][:5]), parent=self)

    # ---------------------------------------------------------------- queue (GUI thread)
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "point":
                    run = self.runs[-1]
                    run["rows"].append(data)
                    k, tt, _, v, i, hit, c, R, over = data
                    src_v = run["src_v"]
                    self.v_readV.set("OVER RANGE" if (over and not src_v) else fmt_si(v, "V"))
                    self.v_readI.set("OVER RANGE" if (over and src_v) else
                                     fmt_si(i, "A") + ("  ⚠" if hit else ""))
                    self.v_readR.set(fmt_si(R, "Ω"))
                    if run["kind"] == "time":
                        if run["cfg"]["duration"] > 0:
                            self.pbar["value"] = min(tt, run["cfg"]["duration"])
                        self.v_points.set(f"{k + 1}  ({fmt_clock(tt)})")
                    else:
                        self.pbar["value"] = k + 1
                        pts = f"{k + 1} / {int(self.pbar['maximum'])}"
                        ncyc = run["cfg"]["cycles"]
                        self.v_points.set(pts + (f"  (cycle {c}/{ncyc})" if ncyc > 1 else ""))
                    self.dirty = True
                elif kind == "idn":
                    if self.runs:
                        self.runs[-1]["idn"] = data
                    note = getattr(self, "run_note", "")
                    self.v_status.set((note + " — " if note else "") + f"Running — {data}")
                elif kind == "status":
                    self.v_status.set(data)
                elif kind == "error":
                    self.v_status.set(data.replace("\n", " "))
                    messagebox.showerror("Error", data, parent=self)
                elif kind == "warn":
                    if self.seq_state is not None:      # don't block an unattended queue
                        self.seq_state["warnings"].append(data)
                        self.v_status.set(data.split("\n")[0])
                    else:
                        self.v_status.set(data.split("\n")[0])
                        messagebox.showwarning("Instrument warning", data, parent=self)
                elif kind == "done":
                    self._on_run_done(data)
        except queue.Empty:
            pass
        if self.dirty:
            self._redraw()
            self.dirty = False
        self.after(50, self._poll)

    def _on_run_done(self, outcome):
        self.busy = False
        self.pbar.stop()
        self.pbar.config(mode="determinate")
        run = self.runs[-1] if self.runs else None
        if run and run.get("discard"):
            self._discard_run(run)
            run = None
            if self.seq_state is not None:
                self.seq_state["discarded"] = True
        if run and run["rows"]:
            self._redraw()
            n_comp = sum(r[5] for r in run["rows"])
            n_over = sum(r[8] for r in run["rows"])
            extra = "".join([f"  ({n_comp} point(s) in compliance)" if n_comp else "",
                             f"  ({n_over} point(s) over range)" if n_over else ""])
            if run["base"]:
                try:
                    self._save_png(run, run["base"] + ".png")
                    self.v_status.set(self.v_status.get() + f" — saved {run['base']}.csv" + extra)
                except Exception as e:
                    log.error("PNG save failed:\n%s", traceback.format_exc())
                    self.v_status.set(self.v_status.get() + f" — CSV saved, PNG failed: {e}")
            else:
                self.v_status.set(self.v_status.get() + extra)
        if self.seq_state is not None:
            self._seq_after_run(outcome)
        else:
            self._set_running(False)
            self._update_preview()

    # ---------------------------------------------------------------- saving
    def _save_png(self, run, path):
        """Publication-style figure: white background, black frame, Arial,
        no grid, outward ticks with minor ticks, SI-prefixed units. No fit line."""
        from matplotlib.ticker import AutoMinorLocator, MaxNLocator
        logx, logy = self._scale_flags()
        by_cycle = self.v_cyclecolor.get()
        kw = self._line_kwargs()
        rc = {"font.family": "sans-serif",
              "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
              "mathtext.default": "regular", "axes.linewidth": 1.5}
        with matplotlib.rc_context(rc):
            fig = Figure(figsize=(5.4, 4.6), dpi=200)
            fig.set_facecolor("white")
            ax = fig.add_subplot(111)
            segs = self._segments(run, by_cycle)
            xq, xunit, yq, yunit = self._axes_info(run)
            sx, xu, sy, yu = self._scaled(segs, xunit, yunit, logx, logy)
            colors = self._cycle_colors(len(segs), PUB_COLORS)
            for k, (c, x, y) in enumerate(segs):
                xx, yy = self._transform(x, y, sx, sy, logx, logy)
                ax.plot(xx, yy, color=colors[k], label=f"Cycle {c}" if c is not None else None,
                        linestyle=kw["linestyle"], marker=kw["marker"],
                        linewidth=kw["lw"], markersize=kw["ms"])
            if logx:
                ax.set_xscale("log")
            if logy:
                ax.set_yscale("log")
            xl, yl = self._labels(xq, xu, yq, yu, run["kind"], logx, logy)
            ax.set_xlabel(xl, fontsize=18, labelpad=6)
            ax.set_ylabel(yl, fontsize=18, labelpad=6)
            for sp in ax.spines.values():
                sp.set_color("black")
                sp.set_linewidth(1.5)
            ax.tick_params(which="both", direction="out", top=False, right=False,
                           labelsize=14, colors="black")
            ax.tick_params(which="major", length=6, width=1.5)
            ax.tick_params(which="minor", length=3.5, width=1.2)
            if not logx:
                ax.xaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
                ax.xaxis.set_minor_locator(AutoMinorLocator(2))
                ax.ticklabel_format(axis="x", useOffset=False)
            if not logy:
                ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
                ax.yaxis.set_minor_locator(AutoMinorLocator(2))
                ax.ticklabel_format(axis="y", useOffset=False)
            ax.margins(x=0.12 if not logx else 0.05, y=0.08)
            if segs[0][0] is not None:
                ax.legend(frameon=False, fontsize=11, ncol=2 if len(segs) > 8 else 1)
            fig.tight_layout()
            fig.savefig(path, facecolor="white")

    def _write_csv(self, run, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(csv_header(run["cfg"], run["idn"], run["started"]))
            w = csv.writer(f)
            w.writerow(COLUMNS)
            for row in run["rows"]:
                w.writerow(csv_cells(row))

    def save_as(self):
        if not self.runs or not self.runs[-1]["rows"]:
            messagebox.showinfo("Nothing to save", "Run a measurement first.", parent=self)
            return
        run = self.runs[-1]
        try:
            suggested = next_name(self.v_folder.get() or os.getcwd(), self._pattern(),
                                  name_tokens(run["cfg"], run["started"]))
        except Exception:
            suggested = safe_part(run["cfg"]["sample"]) + run["started"].strftime("_%Y-%m-%d_%H-%M-%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialdir=self.v_folder.get(), initialfile=suggested + ".csv")
        if path:
            try:
                self._write_csv(run, path)
                self._save_png(run, os.path.splitext(path)[0] + ".png")
                self.v_status.set(f"Saved {path}")
            except Exception as e:
                log.error("Save as failed:\n%s", traceback.format_exc())
                messagebox.showerror("Save failed", f"{e}{hint_for(e)}", parent=self)

    # ---------------------------------------------------------------- closing
    def _on_close(self):
        if self.busy or self.seq_state is not None:
            if not messagebox.askyesno("Measurement running", "Stop the measurement and quit?",
                                       parent=self):
                return
            self.seq_state = None
            self.stop_evt.set()
            self.after(200, self._wait_close)
        else:
            self._save_settings()
            self.destroy()

    def _wait_close(self):
        if self.worker and self.worker.is_alive():
            self.after(200, self._wait_close)
        else:
            self._save_settings()
            self.destroy()


if __name__ == "__main__":
    try:                                    # sharp text on Windows high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
