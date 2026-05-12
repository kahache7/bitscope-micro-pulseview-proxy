# ============================================================
# Configuración portable BitScope
# ============================================================
#
# Portable:
#   Coloca BitLib.dll en la misma carpeta que bitscope_core.py:
#
#       app/
#         bitscope_core.py
#         BitLib.dll
#
# Este proyecto usa BitLib.dll de 32 bits, así que debe ejecutarse
# con Python 32-bit.
#
# Si quieres usar otra DLL, cambia BITLIB_PATH manualmente aquí.
# No se hace búsqueda automática a propósito.

import ctypes
import csv
import html
import math
import os
import subprocess
import time
from pathlib import Path

# ============================================================
# Configuración local
# ============================================================

APP_DIR = Path(__file__).resolve().parent

# Use a local BitLib.dll next to bitscope_core.py by default.
# If the repository is public, keep the private runtime in an ignored
# local folder such as private-package/ and set BITLIB_PATH if needed.
DEFAULT_BITLIB = APP_DIR / "BitLib.dll"
if not DEFAULT_BITLIB.exists():
    PRIVATE_BITLIB = APP_DIR.parent / "private-package" / "BitLib.dll"
    if PRIVATE_BITLIB.exists():
        DEFAULT_BITLIB = PRIVATE_BITLIB

BITLIB_PATH = str(Path(os.environ.get("BITLIB_PATH", str(DEFAULT_BITLIB))).resolve())

# Link del BitScope. Ajustar si cambia el COM.
BITSCOPE_LINK = b"USB:COM2"

SIGROK_CLI_PATH = r"C:\Program Files\sigrok\sigrok-cli\sigrok-cli.exe"


# ============================================================
# Constantes BitLib
# ============================================================

BL_MODE_FAST = 0
BL_MODE_DUAL = 1
BL_MODE_MIXED = 2
BL_MODE_LOGIC = 3

BL_SELECT_CHANNEL = 1
BL_SELECT_SOURCE = 2

BL_SOURCE_POD = 0
BL_SOURCE_BNC = 1
BL_SOURCE_X10 = 2
BL_SOURCE_X20 = 3
BL_SOURCE_X50 = 4
BL_SOURCE_ALT = 5
BL_SOURCE_GND = 6

BL_SOURCE_OPTIONS = {
    "POD": BL_SOURCE_POD,
    "BNC": BL_SOURCE_BNC,
    "X10": BL_SOURCE_X10,
    "X20": BL_SOURCE_X20,
    "X50": BL_SOURCE_X50,
    "ALT": BL_SOURCE_ALT,
    "GND": BL_SOURCE_GND,
}

BL_RANGE_OPTIONS = {
    "1.1 V": 0,
    "3.5 V": 1,
    "5.2 V": 2,
    "9.2 V": 3,
}

BL_RANGE_LABELS = {
    0: "1.1 V",
    1: "3.5 V",
    2: "5.2 V",
    3: "9.2 V",
}

# Probe scaling is applied in software so CSV/SVG/SR/PulseView show
# the physical circuit voltage, not just the attenuated voltage seen by BitScope.
ANALOG_PROBE_OPTIONS = {
    "1x": 1.0,
    "10x": 10.0,
    "custom": 1.0,
}

BL_TRIG_RISE = 0
BL_TRIG_FALL = 1
BL_TRIG_HIGH = 2
BL_TRIG_LOW = 3
BL_TRIG_NONE = 4

BL_TRIGGER_OPTIONS = {
    "RISE": BL_TRIG_RISE,
    "FALL": BL_TRIG_FALL,
    "HIGH": BL_TRIG_HIGH,
    "LOW": BL_TRIG_LOW,
    "NONE": BL_TRIG_NONE,
}


# ============================================================
# Mapas de modos
# ============================================================

PHYSICAL_DIGITAL_CHANNELS = {
    "dig0": 4,
    "dig1": 5,
    "dig2": 6,
    "dig3": 7,
    "dig4": 8,
    "dig5": 9,
}

MODE_CONFIGS = {
    "FAST": {
        "mode_value": BL_MODE_FAST,
        "analog": {"ana0": 0},
        "digital": {},
        "default_rate": "100000",
    },
    "DUAL": {
        "mode_value": BL_MODE_DUAL,
        "analog": {"ana0": 0, "ana1": 1},
        "digital": {},
        "default_rate": "10000",
    },
    "MIXED": {
        "mode_value": BL_MODE_MIXED,
        "analog": {"ana0": 0, "ana1": 1},
        "digital": PHYSICAL_DIGITAL_CHANNELS,
        "default_rate": "10000",
    },
    "LOGIC": {
        "mode_value": BL_MODE_LOGIC,
        "analog": {},
        "digital": PHYSICAL_DIGITAL_CHANNELS,
        "default_rate": "1000000",
    },
}

RATE_OPTIONS = {
    "FAST": [
        "100", "500", "1000", "5000", "10000", "50000",
        "100000", "500000", "1000000", "2000000",
        "5000000", "10000000", "20000000",
    ],
    "DUAL": [
        "100", "500", "1000", "5000", "10000", "50000",
        "100000", "500000", "1000000", "2000000", "5000000",
    ],
    "MIXED": [
        "100", "500", "1000", "5000", "10000", "50000",
        "100000", "500000", "1000000", "2000000",
        "5000000", "10000000",
    ],
    "LOGIC": [
        "2441", "5000", "10000", "50000", "100000", "500000",
        "1000000", "2000000", "5000000", "10000000",
        "20000000", "40000000",
    ],
}

MODE_MAX_SAMPLES = {
    "FAST": 12288,
    "DUAL": 6144,
    "MIXED": 6144,
    "LOGIC": 12288,
}

ACTUAL_RATE_HINTS = {
    ("LOGIC", "2441"): 2441.40625,
}


# ============================================================
# Ayudas de límites
# ============================================================

def get_actual_rate_hint(mode_name, requested_rate_text):
    key = (mode_name, str(requested_rate_text))
    if key in ACTUAL_RATE_HINTS:
        return ACTUAL_RATE_HINTS[key]
    return float(requested_rate_text)


def get_max_capture_time_hint(mode_name, requested_rate_text):
    max_samples = MODE_MAX_SAMPLES[mode_name]
    actual_rate = get_actual_rate_hint(mode_name, requested_rate_text)

    if actual_rate <= 0:
        return 0.0

    return max_samples / actual_rate


def describe_modes():
    rows = []

    for mode_name in MODE_CONFIGS:
        for rate_text in RATE_OPTIONS[mode_name]:
            actual_rate = get_actual_rate_hint(mode_name, rate_text)
            max_samples = MODE_MAX_SAMPLES[mode_name]
            max_time = max_samples / actual_rate

            rows.append({
                "mode": mode_name,
                "rate_text": rate_text,
                "actual_rate": actual_rate,
                "max_samples": max_samples,
                "max_time": max_time,
            })

    return rows


# ============================================================
# Carga de BitLib
# ============================================================

def load_bitlib():
    dll = ctypes.CDLL(BITLIB_PATH)

    dll.BL_Open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    dll.BL_Open.restype = ctypes.c_int

    dll.BL_Close.argtypes = []
    dll.BL_Close.restype = None

    dll.BL_Mode.argtypes = [ctypes.c_int]
    dll.BL_Mode.restype = ctypes.c_int

    dll.BL_Select.argtypes = [ctypes.c_int, ctypes.c_int]
    dll.BL_Select.restype = ctypes.c_int

    dll.BL_Range.argtypes = [ctypes.c_int]
    dll.BL_Range.restype = ctypes.c_double

    dll.BL_Offset.argtypes = [ctypes.c_double]
    dll.BL_Offset.restype = ctypes.c_double

    dll.BL_Intro.argtypes = [ctypes.c_double]
    dll.BL_Intro.restype = ctypes.c_double

    dll.BL_Delay.argtypes = [ctypes.c_double]
    dll.BL_Delay.restype = ctypes.c_double

    dll.BL_Enable.argtypes = [ctypes.c_bool]
    dll.BL_Enable.restype = ctypes.c_bool

    dll.BL_Trigger.argtypes = [ctypes.c_double, ctypes.c_int]
    dll.BL_Trigger.restype = ctypes.c_bool

    dll.BL_Size.argtypes = [ctypes.c_int]
    dll.BL_Size.restype = ctypes.c_int

    dll.BL_Rate.argtypes = [ctypes.c_double]
    dll.BL_Rate.restype = ctypes.c_double

    dll.BL_Index.argtypes = [ctypes.c_int]
    dll.BL_Index.restype = ctypes.c_int

    dll.BL_Trace.argtypes = [ctypes.c_double, ctypes.c_bool]
    dll.BL_Trace.restype = ctypes.c_bool

    dll.BL_Acquire.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_double)]
    dll.BL_Acquire.restype = ctypes.c_int

    dll.BL_Version.argtypes = [ctypes.c_int]
    dll.BL_Version.restype = ctypes.c_char_p

    dll.BL_ID.argtypes = []
    dll.BL_ID.restype = ctypes.c_char_p

    return dll


def bstr(x):
    if not x:
        return ""
    return x.decode(errors="replace")


# ============================================================
# Captura
# ============================================================

def setup_channel(
    dll,
    channel,
    source=BL_SOURCE_POD,
    range_index=3,
    offset=0.0,
):
    dll.BL_Select(BL_SELECT_CHANNEL, channel)

    actual_source = dll.BL_Select(BL_SELECT_SOURCE, int(source))

    try:
        actual_range = dll.BL_Range(int(range_index))
        actual_offset = dll.BL_Offset(float(offset))
    except Exception:
        actual_range = None
        actual_offset = None

    enabled = dll.BL_Enable(True)

    return {
        "enabled": bool(enabled),
        "requested_source": int(source),
        "actual_source": int(actual_source),
        "source_ok": int(actual_source) == int(source),
        "requested_range_index": int(range_index),
        "actual_range": actual_range,
        "requested_offset": float(offset),
        "actual_offset": actual_offset,
    }


def acquire_channel(dll, channel, n):
    dll.BL_Select(BL_SELECT_CHANNEL, channel)
    dll.BL_Index(0)

    buf_type = ctypes.c_double * n
    buf = buf_type()

    got = dll.BL_Acquire(n, buf)
    return [buf[i] for i in range(got)]


def digitalize(values):
    return [1 if v >= 2.5 else 0 for v in values]


def analog_to_digital(values, threshold):
    return [1 if v >= threshold else 0 for v in values]


def resolve_trigger_channel(mode_config, source_name):
    """Resolve a trigger source name like ana0, ana1, dig0... to a BitLib channel.

    BitLib triggers are simple hardware triggers on one selected channel.
    Complex multi-channel triggers should be built later as software-qualified
    repeated captures or with an external trigger line.
    """
    if source_name in mode_config["analog"]:
        return "analog", mode_config["analog"][source_name]

    if source_name in mode_config["digital"]:
        return "digital", mode_config["digital"][source_name]

    known = list(mode_config["analog"].keys()) + list(mode_config["digital"].keys())
    raise ValueError(
        f"Canal de trigger no disponible en este modo: {source_name}. "
        f"Disponibles: {', '.join(known)}"
    )


def default_trigger_source(mode_config):
    if mode_config["analog"]:
        return next(iter(mode_config["analog"].keys()))
    if mode_config["digital"]:
        return next(iter(mode_config["digital"].keys()))
    raise RuntimeError("No hay canales disponibles para trigger.")


def normalize_trigger_config(trigger_config):
    """Normalize user/proxy trigger config.

    Supported modes:
      - forced/free-run/disabled: BL_Trace(0.0, False)
      - normal/auto: BL_Trigger(...) + BL_Trace(timeout_s, False)

    Observed BitLib behavior on this BitScope Micro is AUTO-like: if no real
    trigger happens, BL_Trace(timeout, False) may still return True with a
    free-running capture after timeout. Use trigger_status/event_index as a
    heuristic, not as a hardware guarantee.
    """
    if not trigger_config:
        return {"enabled": False, "mode": "forced"}

    cfg = dict(trigger_config)
    mode = str(cfg.get("mode", "normal")).strip().lower()
    forced_modes = {"forced", "free-run", "freerun", "none", "disabled", "off"}
    enabled = bool(cfg.get("enabled", mode not in forced_modes))

    if not enabled or mode in forced_modes:
        return {"enabled": False, "mode": "forced"}

    edge = str(cfg.get("edge", "RISE")).strip().upper()
    if edge not in BL_TRIGGER_OPTIONS:
        raise ValueError(f"Trigger edge no soportado: {edge}")

    timeout_s = float(cfg.get("timeout_s", 0.5))
    if timeout_s <= 0:
        timeout_s = 0.5

    return {
        "enabled": True,
        "mode": mode,
        "source": cfg.get("source", None),
        "edge": edge,
        "level": float(cfg.get("level", 1.0)),
        "intro_s": float(cfg.get("intro_s", 0.0)),
        "delay_s": float(cfg.get("delay_s", 0.0)),
        "timeout_s": timeout_s,
    }


def default_trigger_result(enabled=False):
    return {
        "enabled": bool(enabled),
        "mode": "forced",
        "source": None,
        "edge": None,
        "level": None,
        "level_bitscope": None,
        "intro_s": 0.0,
        "actual_intro_s": 0.0,
        "delay_s": 0.0,
        "actual_delay_s": 0.0,
        "timeout_s": 0.0,
        "trigger_channel": None,
        "trigger_kind": None,
        "bl_trigger_ok": None,
        "trace_ok": None,
        "elapsed_s": None,
        "trigger_status": "forced",
        "event_index": None,
        "event_time_s": None,
    }


def find_trigger_like_index(values, level, edge_name):
    """Find the first event in an acquired buffer matching the trigger shape."""
    if not values:
        return None

    edge_name = str(edge_name).upper()

    if edge_name == "RISE":
        for i in range(1, len(values)):
            if values[i - 1] < level <= values[i]:
                return i

    elif edge_name == "FALL":
        for i in range(1, len(values)):
            if values[i - 1] > level >= values[i]:
                return i

    elif edge_name == "HIGH":
        for i, v in enumerate(values):
            if v >= level:
                return i

    elif edge_name == "LOW":
        for i, v in enumerate(values):
            if v <= level:
                return i

    return None


def infer_trigger_status(trace_ok, elapsed_s, timeout_s, event_index):
    """Heuristic trigger status for BitLib's observed AUTO-like behavior."""
    if not trace_ok:
        return "trace_failed"

    if timeout_s <= 0:
        return "forced"

    if elapsed_s < timeout_s * 0.85:
        if event_index is not None:
            return "likely_triggered"
        return "likely_triggered_no_event_found"

    if event_index is not None:
        return "timeout_auto_with_event_in_buffer"

    return "timeout_auto_no_event_found"


def summarize(name, data):
    if not data:
        return f"{name}: no data"

    return (
        f"{name}: n={len(data)} "
        f"min={min(data):.4g} "
        f"max={max(data):.4g} "
        f"vpp={(max(data) - min(data)):.4g} "
        f"avg={(sum(data) / len(data)):.4g}"
    )


# ============================================================
# Exportadores
# ============================================================

def save_full_csv(filename, times, analog, digital):
    names = list(analog.keys()) + list(digital.keys())

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sample", "time_s"] + names)

        for i, t in enumerate(times):
            row = [i, t]

            for name in analog:
                row.append(analog[name][i])

            for name in digital:
                row.append(digital[name][i])

            writer.writerow(row)


def save_sigrok_csv(filename, times, analog, digital):
    names = list(analog.keys()) + list(digital.keys())

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time"] + names)

        for i, t in enumerate(times):
            row = [t]

            for name in analog:
                row.append(analog[name][i])

            for name in digital:
                row.append(digital[name][i])

            writer.writerow(row)


def sigrok_column_format(analog, digital):
    fields = ["t"]
    fields.extend(["a"] * len(analog))
    fields.extend(["l"] * len(digital))
    return ",".join(fields)


def save_vcd(filename, times, digital):
    if not digital:
        return

    symbols = ["!", '"', "#", "$", "%", "&", "'", "(", ")", "*", "+", ","]
    names = list(digital.keys())
    times_us = [int(round(t * 1_000_000)) for t in times]

    with open(filename, "w", newline="\n") as f:
        f.write("$date\n")
        f.write("  generated by BitScope Python tools\n")
        f.write("$end\n")
        f.write("$version\n")
        f.write("  BitScope export\n")
        f.write("$end\n")
        f.write("$timescale 1 us $end\n")
        f.write("$scope module bitscope $end\n")

        for name, symbol in zip(names, symbols):
            f.write(f"$var wire 1 {symbol} {name} $end\n")

        f.write("$upscope $end\n")
        f.write("$enddefinitions $end\n")

        previous = {}

        for i, t_us in enumerate(times_us):
            changes = []

            for name, symbol in zip(names, symbols):
                value = digital[name][i]
                if previous.get(name) != value:
                    changes.append(f"{value}{symbol}")
                    previous[name] = value

            if changes:
                f.write(f"#{t_us}\n")
                for change in changes:
                    f.write(change + "\n")


def make_polyline(times, values, x0, y0, width, height, t_min, t_max, v_min, v_max):
    if t_max == t_min:
        t_max = t_min + 1.0

    if v_max == v_min:
        v_max = v_min + 1.0

    points = []

    for t, v in zip(times, values):
        x = x0 + (t - t_min) / (t_max - t_min) * width
        y = y0 + height - (v - v_min) / (v_max - v_min) * height
        points.append(f"{x:.2f},{y:.2f}")

    return " ".join(points)


def save_svg(filename, times, analog, digital):
    width = 1300
    height = 850

    margin_left = 95
    margin_right = 30
    margin_top = 60
    margin_bottom = 45

    plot_x = margin_left
    plot_w = width - margin_left - margin_right

    has_analog = bool(analog)
    has_digital = bool(digital)

    if has_analog and has_digital:
        analog_y = margin_top
        analog_h = 340
        digital_y = analog_y + analog_h + 80
        digital_h = height - digital_y - margin_bottom
    elif has_analog:
        analog_y = margin_top
        analog_h = height - margin_top - margin_bottom
        digital_y = 0
        digital_h = 0
    else:
        analog_y = 0
        analog_h = 0
        digital_y = margin_top
        digital_h = height - margin_top - margin_bottom

    t_min = min(times)
    t_max = max(times)

    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
        "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
        "#bcbd22", "#17becf", "#006666", "#990099",
    ]

    color_map = {}
    for i, name in enumerate(list(analog.keys()) + list(digital.keys())):
        color_map[name] = colors[i % len(colors)]

    grid = []

    grid_top = analog_y if has_analog else digital_y
    grid_bottom = digital_y + digital_h if has_digital else analog_y + analog_h

    for i in range(11):
        x = plot_x + plot_w * i / 10
        t = t_min + (t_max - t_min) * i / 10

        grid.append(
            f'<line x1="{x:.2f}" y1="{grid_top}" x2="{x:.2f}" '
            f'y2="{grid_bottom}" stroke="#eee" />'
        )
        grid.append(
            f'<text x="{x:.2f}" y="{height - 14}" text-anchor="middle" '
            f'font-size="12">{t:.6g}s</text>'
        )

    analog_lines = []

    if has_analog:
        values_all = []
        for values in analog.values():
            values_all.extend(values)

        v_min = min(values_all)
        v_max = max(values_all)

        padding = (v_max - v_min) * 0.08
        if padding == 0:
            padding = 1.0

        v_min -= padding
        v_max += padding

        for i in range(9):
            y = analog_y + analog_h * i / 8
            v = v_max - (v_max - v_min) * i / 8

            grid.append(
                f'<line x1="{plot_x}" y1="{y:.2f}" x2="{plot_x + plot_w}" '
                f'y2="{y:.2f}" stroke="#ddd" />'
            )
            grid.append(
                f'<text x="{plot_x - 10}" y="{y + 4:.2f}" text-anchor="end" '
                f'font-size="12">{v:.3g}V</text>'
            )

        for name, values in analog.items():
            points = make_polyline(
                times, values,
                plot_x, analog_y,
                plot_w, analog_h,
                t_min, t_max,
                v_min, v_max,
            )
            analog_lines.append(
                f'<polyline points="{points}" fill="none" '
                f'stroke="{color_map[name]}" stroke-width="1.4"/>'
            )

    digital_lines = []

    if has_digital:
        lane_count = len(digital)
        lane_gap = digital_h / lane_count

        for lane_index, (name, values) in enumerate(digital.items()):
            lane_top = digital_y + lane_gap * lane_index
            lane_mid = lane_top + lane_gap * 0.5

            y_low = lane_top + lane_gap * 0.72
            y_high = lane_top + lane_gap * 0.28

            grid.append(
                f'<line x1="{plot_x}" y1="{lane_mid:.2f}" '
                f'x2="{plot_x + plot_w}" y2="{lane_mid:.2f}" stroke="#eee" />'
            )
            grid.append(
                f'<text x="{plot_x - 10}" y="{lane_mid + 4:.2f}" '
                f'text-anchor="end" font-size="13">{html.escape(name)}</text>'
            )

            points = []
            last_y = None

            for t, value in zip(times, values):
                x = plot_x + (t - t_min) / (t_max - t_min) * plot_w
                y = y_high if value else y_low

                if last_y is not None and y != last_y:
                    points.append(f"{x:.2f},{last_y:.2f}")

                points.append(f"{x:.2f},{y:.2f}")
                last_y = y

            point_str = " ".join(points)

            digital_lines.append(
                f'<polyline points="{point_str}" fill="none" '
                f'stroke="{color_map[name]}" stroke-width="1.4"/>'
            )

    legend_items = []
    lx = plot_x + 10
    ly = 38

    for name in list(analog.keys()) + list(digital.keys()):
        legend_items.append(
            f'<line x1="{lx}" y1="{ly}" x2="{lx + 30}" y2="{ly}" '
            f'stroke="{color_map[name]}" stroke-width="3"/>'
        )
        legend_items.append(
            f'<text x="{lx + 38}" y="{ly + 5}" font-size="13">'
            f'{html.escape(name)}</text>'
        )
        lx += 115

    mode_text = html.escape(", ".join(list(analog.keys()) + list(digital.keys())))

    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <rect width="100%" height="100%" fill="white"/>

  <text x="{width / 2}" y="24" text-anchor="middle" font-size="22" font-family="Arial">
    BitScope capture
  </text>

  <text x="{width / 2}" y="48" text-anchor="middle" font-size="12" font-family="Arial">
    {mode_text}
  </text>

  {"".join(legend_items)}
  {"".join(grid)}

  {"".join(analog_lines)}
  {"".join(digital_lines)}

  {f'<rect x="{plot_x}" y="{analog_y}" width="{plot_w}" height="{analog_h}" fill="none" stroke="#444" />' if has_analog else ''}
  {f'<rect x="{plot_x}" y="{digital_y}" width="{plot_w}" height="{digital_h}" fill="none" stroke="#444" />' if has_digital else ''}
</svg>
'''

    with open(filename, "w", encoding="utf-8") as f:
        f.write(svg)


def generate_sr(sigrok_csv_file, sr_file, analog, digital, log):
    fmt = sigrok_column_format(analog, digital)

    cmd = [
        SIGROK_CLI_PATH,
        "-I", f"csv:header=yes:column_formats={fmt}",
        "-i", str(sigrok_csv_file),
        "-O", "srzip",
        "-o", str(sr_file),
    ]

    log("sigrok-cli: " + " ".join(f'"{x}"' if " " in x else x for x in cmd))

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.stdout.strip():
        log(result.stdout.strip())

    if result.stderr.strip():
        log(result.stderr.strip())

    if result.returncode != 0:
        raise RuntimeError(f"sigrok-cli terminó con código {result.returncode}")


# ============================================================
# Motor principal
# ============================================================

def capture_to_files(
    mode_name,
    requested_rate,
    requested_time,
    output_prefix,
    make_csv=False,
    make_sigrok_csv=False,
    make_svg=False,
    make_vcd=False,
    make_sr=True,
    analog_digital_threshold=1.5,
    trigger_config=None,
    analog_source=BL_SOURCE_POD,
    analog_range_index=3,
    analog_offset=0.0,
    analog_probe_factor=1.0,
    analog_channel_settings=None,
    digital_source=BL_SOURCE_POD,
    digital_range_index=3,
    digital_offset=0.0,
    log=print,
):
    config = MODE_CONFIGS[mode_name]

    def _analog_setting(name):
        """Return per-analog-channel acquisition/scaling settings.

        Backward compatible defaults match the old global parameters.
        Keys accepted per channel: source, range_index, offset, probe_factor.
        """
        channel_cfg = (analog_channel_settings or {}).get(name, {})
        return {
            "source": channel_cfg.get("source", analog_source),
            "range_index": channel_cfg.get("range_index", analog_range_index),
            "offset": channel_cfg.get("offset", analog_offset),
            "probe_factor": channel_cfg.get("probe_factor", analog_probe_factor),
        }

    dll = load_bitlib()

    opened = dll.BL_Open(BITSCOPE_LINK, 1)
    log(f"opened = {opened}")

    if opened <= 0:
        raise RuntimeError("No se pudo abrir el BitScope.")

    try:
        log(f"BitScope ID: {bstr(dll.BL_ID())}")
        log(f"BitLib: {bstr(dll.BL_Version(1))}")

        selected_mode = dll.BL_Mode(config["mode_value"])
        log(f"selected mode = {selected_mode}")

        if selected_mode != config["mode_value"]:
            raise RuntimeError(f"Modo {mode_name} no aceptado por BitScope.")

        dll.BL_Intro(0.0)
        dll.BL_Delay(0.0)

        analog_settings_used = {}

        for name, ch in config["analog"].items():
            acfg = _analog_setting(name)
            analog_settings_used[name] = acfg
            enabled = setup_channel(
                dll,
                ch,
                source=acfg["source"],
                range_index=acfg["range_index"],
                offset=acfg["offset"],
            )
            log(
                f"setup analog {name} channel {ch}: "
                f"source={acfg['source']}, range_index={acfg['range_index']}, "
                f"offset={acfg['offset']}, probe_factor={acfg['probe_factor']}, "
                f"enable={enabled}"
            )

        for name, ch in config["digital"].items():
            enabled = setup_channel(
                dll,
                ch,
                source=digital_source,
                range_index=digital_range_index,
                offset=digital_offset,
            )
            log(
                f"setup digital {name} channel {ch}: "
                f"source={digital_source}, range_index={digital_range_index}, "
                f"offset={digital_offset}, enable={enabled}"
            )

        actual_rate = dll.BL_Rate(float(requested_rate))
        max_samples = dll.BL_Size(0)

        if requested_time and requested_time > 0:
            requested_samples = int(math.ceil(requested_time * actual_rate))
            requested_samples = max(1, min(requested_samples, max_samples))
        else:
            requested_samples = max_samples

        actual_size = dll.BL_Size(requested_samples)
        actual_time = actual_size / actual_rate

        log(f"requested rate = {requested_rate} Hz")
        log(f"actual rate    = {actual_rate} Hz")
        log(f"max samples    = {max_samples}")
        log(f"actual samples = {actual_size}")
        log(f"capture time   = {actual_time} s")
        if config["analog"]:
            for name in config["analog"]:
                acfg = analog_settings_used.get(name, _analog_setting(name))
                log(f"analog {name} probe = {float(acfg['probe_factor'])}x")

        trig_cfg = normalize_trigger_config(trigger_config)
        trigger_result = default_trigger_result(enabled=trig_cfg.get("enabled", False))

        if trig_cfg.get("enabled"):
            trigger_source = trig_cfg.get("source") or default_trigger_source(config)
            trigger_kind, trigger_channel = resolve_trigger_channel(config, trigger_source)
            edge_name = trig_cfg["edge"]

            trigger_level_physical = float(trig_cfg["level"])
            trigger_level_bitscope = trigger_level_physical

            # The GUI/user level is expressed in physical circuit volts.
            # BitLib sees the attenuated voltage at BitScope input, so for
            # analog channels with a 10x/custom probe we divide by probe_factor.
            if trigger_kind == "analog":
                acfg = analog_settings_used.get(trigger_source, _analog_setting(trigger_source))
                probe_factor = float(acfg.get("probe_factor", 1.0))
                if probe_factor != 0.0:
                    trigger_level_bitscope = trigger_level_physical / probe_factor

            actual_intro = dll.BL_Intro(float(trig_cfg["intro_s"]))
            actual_delay = dll.BL_Delay(float(trig_cfg["delay_s"]))

            dll.BL_Select(BL_SELECT_CHANNEL, int(trigger_channel))
            trig_ok = dll.BL_Trigger(
                float(trigger_level_bitscope),
                int(BL_TRIGGER_OPTIONS[edge_name]),
            )

            trace_timeout = max(0.0, float(trig_cfg["timeout_s"]))

            log("Trigger config")
            log("--------------")
            log(f"trigger mode       = {trig_cfg['mode']}")
            log(f"trigger source     = {trigger_source} channel={trigger_channel} kind={trigger_kind}")
            log(f"trigger edge       = {edge_name}")
            log(f"trigger level phys = {trigger_level_physical:g} V")
            log(f"trigger level BS   = {trigger_level_bitscope:g} V")
            log(f"intro req/actual   = {trig_cfg['intro_s']:g} / {actual_intro:g} s")
            log(f"delay req/actual   = {trig_cfg['delay_s']:g} / {actual_delay:g} s")
            log(f"timeout            = {trace_timeout:g} s")
            log(f"BL_Trigger ok      = {trig_ok}")

            trigger_result.update({
                "enabled": True,
                "mode": trig_cfg["mode"],
                "source": trigger_source,
                "edge": edge_name,
                "level": trigger_level_physical,
                "level_bitscope": trigger_level_bitscope,
                "intro_s": float(trig_cfg["intro_s"]),
                "actual_intro_s": float(actual_intro),
                "delay_s": float(trig_cfg["delay_s"]),
                "actual_delay_s": float(actual_delay),
                "timeout_s": trace_timeout,
                "trigger_channel": trigger_channel,
                "trigger_kind": trigger_kind,
                "bl_trigger_ok": bool(trig_ok),
            })

        else:
            actual_intro = dll.BL_Intro(0.0)
            actual_delay = dll.BL_Delay(0.0)
            trace_timeout = 0.0

            log("Trigger config")
            log("--------------")
            log("trigger mode       = forced/free-run")
            log("BL_Trace timeout   = 0.0")

            trigger_result.update({
                "enabled": False,
                "mode": "forced",
                "actual_intro_s": float(actual_intro),
                "actual_delay_s": float(actual_delay),
                "timeout_s": trace_timeout,
            })

        log("Starting trace...")
        t0 = time.time()
        ok = dll.BL_Trace(float(trace_timeout), False)
        elapsed = time.time() - t0

        log(f"trace = {ok}")
        log(f"elapsed = {elapsed:.4f} s")

        trigger_result.update({
            "trace_ok": bool(ok),
            "elapsed_s": elapsed,
        })

        if not ok:
            trigger_result["trigger_status"] = "trace_failed"
            raise RuntimeError("BL_Trace falló.")

        raw_analog = {}
        raw_digital = {}

        for name, ch in config["analog"].items():
            values = acquire_channel(dll, ch, actual_size)

            # BitLib gives the voltage seen by BitScope. If the physical probe is
            # set to x10, x20, etc., scale here so all outputs show the real
            # circuit voltage. Default 1.0 preserves previous behavior.
            probe_factor = float(analog_settings_used.get(name, _analog_setting(name))["probe_factor"])
            if probe_factor != 1.0:
                values = [v * probe_factor for v in values]

            raw_analog[name] = values
            log(summarize(name, raw_analog[name]))

        for name, ch in config["digital"].items():
            values = acquire_channel(dll, ch, actual_size)
            raw_digital[name] = digitalize(values)
            log(summarize(name, values))

        if mode_name == "MIXED":
            if "ana0" in raw_analog:
                raw_digital["ana0_dig"] = analog_to_digital(
                    raw_analog["ana0"],
                    analog_digital_threshold,
                )
                log(f"ana0_dig: derived from ana0 >= {analog_digital_threshold} V")

            if "ana1" in raw_analog:
                raw_digital["ana1_dig"] = analog_to_digital(
                    raw_analog["ana1"],
                    analog_digital_threshold,
                )
                log(f"ana1_dig: derived from ana1 >= {analog_digital_threshold} V")

        if trig_cfg.get("enabled"):
            event_index = None
            event_time = None
            trigger_source = trigger_result.get("source")
            edge_name = trigger_result.get("edge")

            if trigger_source in raw_analog:
                # raw_analog is already scaled by probe_factor, so use the
                # physical trigger level requested by the user.
                analysis_level = float(trigger_result.get("level"))
                event_index = find_trigger_like_index(
                    raw_analog[trigger_source],
                    analysis_level,
                    edge_name,
                )

            elif trigger_source in raw_digital:
                # raw_digital is logical 0/1 after digitalize(), so use 0.5
                # for edge/high/low detection regardless of electrical threshold.
                analysis_level = 0.5
                event_index = find_trigger_like_index(
                    raw_digital[trigger_source],
                    analysis_level,
                    edge_name,
                )

            if event_index is not None:
                event_time = event_index / actual_rate

            trigger_status = infer_trigger_status(
                trace_ok=ok,
                elapsed_s=elapsed,
                timeout_s=float(trigger_result.get("timeout_s") or 0.0),
                event_index=event_index,
            )

            trigger_result.update({
                "trigger_status": trigger_status,
                "event_index": event_index,
                "event_time_s": event_time,
            })

            log("Trigger analysis")
            log("----------------")
            log(f"trigger_status = {trigger_status}")
            log(f"event_index    = {event_index}")
            log(f"event_time_s   = {event_time}")

        else:
            trigger_result["trigger_status"] = "forced"

        lengths = []
        for values in raw_analog.values():
            lengths.append(len(values))
        for values in raw_digital.values():
            lengths.append(len(values))

        if not lengths:
            raise RuntimeError("No hay canales activos.")

        n = min(lengths)

        analog = {name: values[:n] for name, values in raw_analog.items()}
        digital = {name: values[:n] for name, values in raw_digital.items()}

        dt = 1.0 / actual_rate
        times = [i * dt for i in range(n)]

        prefix = Path(output_prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)

        full_csv_file = prefix.with_suffix(".csv")
        sigrok_csv_file = prefix.parent / f"{prefix.name}_sigrok.csv"
        svg_file = prefix.with_suffix(".svg")
        vcd_file = prefix.with_suffix(".vcd")
        sr_file = prefix.with_suffix(".sr")

        if make_csv:
            save_full_csv(full_csv_file, times, analog, digital)
            log(f"Saved: {full_csv_file}")

        if make_sigrok_csv or make_sr:
            save_sigrok_csv(sigrok_csv_file, times, analog, digital)
            log(f"Saved: {sigrok_csv_file}")

        if make_svg:
            save_svg(svg_file, times, analog, digital)
            log(f"Saved: {svg_file}")

        if make_vcd and digital:
            save_vcd(vcd_file, times, digital)
            log(f"Saved: {vcd_file}")

        if make_sr:
            if sr_file.exists():
                sr_file.unlink()

            generate_sr(sigrok_csv_file, sr_file, analog, digital, log)
            log(f"Saved: {sr_file}")

            if not make_sigrok_csv and sigrok_csv_file.exists():
                sigrok_csv_file.unlink()
                log(f"Deleted temporary: {sigrok_csv_file}")

        return {
            "actual_rate": actual_rate,
            "actual_size": actual_size,
            "actual_time": actual_time,
            "analog": analog,
            "digital": digital,
            "times": times,
            "trigger": trigger_result,
        }

    finally:
        dll.BL_Close()
        log("closed")