#!/usr/bin/env python3
"""
BitScope Micro -> PulseView direct proxy, first prototype.

What it does:
  - Starts a TCP SCPI server that pretends to be a Rigol MSO2072A.
  - PulseView connects using the existing libsigrok rigol-ds driver:
        pulseview -d rigol-ds:conn=tcp-raw/127.0.0.1/5555 -l 5
  - The proxy captures from BitScope using bitscope_core.py and serves CH1/CH2/LA.

Design choice:
  The user selects BitScope mode and real sample rate in this GUI.
  PulseView is used as viewer/decoder and should be configured as indicated
  in the instructions panel.

Requires:
  - Same folder as bitscope_core.py.
  - Python 3.11 32-bit if using BitLib.dll 32-bit.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import queue
import traceback
import subprocess
import struct
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from bitscope_core import (
    MODE_CONFIGS,
    RATE_OPTIONS,
    get_actual_rate_hint,
    capture_to_files,
    BL_SOURCE_OPTIONS,
    BL_RANGE_OPTIONS,
)

HOST = "127.0.0.1"
PORT = 5555

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "bitscope_proxy_settings.json"

# Rigol/driver chunking observed for MSO2072A / rigol-ds.
LIVE_SAMPLES = 1400
ANALOG_MEMORY_SAMPLES = 7000
LOGIC_MEMORY_SAMPLES = 14000
SEGMENT_SAMPLES = 7000

# Analog byte conversion exposed to rigol-ds.
# PulseView reconstructs volts roughly as: V = (byte - YREF - YOR) * YINC.
ANALOG_YINC = 0.04
ANALOG_YREF = 128
ANALOG_YOR = 0

# Chunk sizes returned per :WAV:DATA?.
ANALOG_CHUNK_BYTES = 1400
LOGIC_CHUNK_BYTES = 2800  # 1400 logic samples * uint16 little-endian

DEFAULT_PULSEVIEW_PATH = r"C:\Program Files\sigrok\PulseView_N\pulseview.exe"
DEFAULT_PULSEVIEW_CMD = f'& "{DEFAULT_PULSEVIEW_PATH}" --dont-scan -d rigol-ds:conn=tcp-raw/{HOST}/{PORT} -l 5'

# GUI deliberately exposes only the two physical analog connection choices we need.
# IMPORTANT for BitScope Micro DB98TI79 / BitLib 2.0 FE26B:
#   BL_SELECT_SOURCE appears to stay on POD even if BNC/X10 is requested.
#   Therefore this GUI field is only a *physical connection note* for the user.
#   The proxy/core capture path forces BitLib source=POD and uses probe_factor
#   separately to show/export the real physical voltage.
GUI_ANALOG_SOURCE_OPTIONS = ["POD", "BNC"]
GUI_PROBE_OPTIONS = ["1x", "10x", "custom"]
GUI_YINC_MODE_OPTIONS = ["auto", "manual"]

# BitLib supports a simple hardware trigger on one selected channel.
# Complex conditions such as "D0 falls and then D1 falls" are left for a
# later software-qualified trigger layer or external trigger generation.
GUI_TRIGGER_MODE_OPTIONS = ["Forced", "Normal", "Auto"]
GUI_TRIGGER_EDGE_OPTIONS = ["RISE", "FALL", "HIGH", "LOW"]


def parse_range_volts(range_label: str) -> float:
    """Extract the numeric volts value from labels like '9.2 V'."""
    return float(str(range_label).split()[0].replace(",", "."))


def auto_yinc_for(range_label: str, probe_factor: float) -> float:
    """Conservative Rigol byte scaling for one analog channel.

    The proxy sends 8-bit analog samples centered around YREF=128.
    Auto YINC maps roughly +/- (BitScope range * probe factor) into the
    available 8-bit display range, avoiding clipping after probe scaling.

    You can still use manual YINC per channel if you want more resolution
    or a larger displayed range.
    """
    try:
        range_v = parse_range_volts(range_label)
        factor = float(probe_factor)
        return max((range_v * factor) / 127.0, 1e-12)
    except Exception:
        return ANALOG_YINC


def probe_factor_from_mode(mode: str, custom_text: str) -> float:
    if mode == "1x":
        return 1.0
    if mode == "10x":
        return 10.0
    factor = float(custom_text)
    if factor <= 0:
        raise ValueError("Probe factor must be positive.")
    return factor


def source_value(source_name: str) -> int:
    return BL_SOURCE_OPTIONS.get(source_name, BL_SOURCE_OPTIONS["POD"])


def range_index(range_label: str) -> int:
    return BL_RANGE_OPTIONS.get(range_label, BL_RANGE_OPTIONS["9.2 V"])


def python_bits() -> int:
    return struct.calcsize("P") * 8

def is_32bit_python() -> bool:
    return python_bits() == 32


# -----------------------------------------------------------------------------
# BitScope limits measured on the user's BitScope Micro
# -----------------------------------------------------------------------------
# requested_rate_hz is what we ask BitLib for.
# actual_rate_hz is what BitLib/BitScope actually gives back.
# The proxy uses actual_rate_hz to compute the real capture duration and WAV:XINC.

PROBED_LIMIT_ROWS = [
    ("FAST", "100", 100.0, 12288, 122.88),
    ("FAST", "500", 500.0, 12288, 24.576),
    ("FAST", "1000", 1000.0, 12288, 12.288),
    ("FAST", "5000", 5000.0, 12288, 2.4576),
    ("FAST", "10000", 10000.0, 12288, 1.2288),
    ("FAST", "50000", 50000.0, 12288, 0.24576),
    ("FAST", "100000", 100000.0, 12288, 0.12288),
    ("FAST", "500000", 500000.0, 12288, 0.024576),
    ("FAST", "1000000", 1000000.0, 12288, 0.012288),
    ("FAST", "2000000", 2000000.0, 12288, 0.006144),
    ("FAST", "5000000", 5000000.0, 12288, 0.0024576),
    ("FAST", "10000000", 10000000.0, 12288, 0.0012288),
    ("FAST", "20000000", 20000000.0, 12288, 0.0006144),
    ("FAST", "40000000", 20000000.0, 12288, 0.0006144),

    ("DUAL", "100", 100.0, 6144, 61.44),
    ("DUAL", "500", 500.0, 6144, 12.288),
    ("DUAL", "1000", 1000.0, 6144, 6.144),
    ("DUAL", "5000", 5000.0, 6144, 1.2288),
    ("DUAL", "10000", 10000.0, 6144, 0.6144),
    ("DUAL", "50000", 50000.0, 6144, 0.12288),
    ("DUAL", "100000", 100000.0, 6144, 0.06144),
    ("DUAL", "500000", 500000.0, 6144, 0.012288),
    ("DUAL", "1000000", 1000000.0, 6144, 0.006144),
    ("DUAL", "2000000", 2000000.0, 6144, 0.003072),
    ("DUAL", "5000000", 5000000.0, 6144, 0.0012288),
    ("DUAL", "10000000", 5000000.0, 6144, 0.0012288),
    ("DUAL", "20000000", 5000000.0, 6144, 0.0012288),
    ("DUAL", "40000000", 5000000.0, 6144, 0.0012288),

    ("MIXED", "100", 100.0, 6144, 61.44),
    ("MIXED", "500", 500.0, 6144, 12.288),
    ("MIXED", "1000", 1000.0, 6144, 6.144),
    ("MIXED", "5000", 5000.0, 6144, 1.2288),
    ("MIXED", "10000", 10000.0, 6144, 0.6144),
    ("MIXED", "50000", 50000.0, 6144, 0.12288),
    ("MIXED", "100000", 100000.0, 6144, 0.06144),
    ("MIXED", "500000", 500000.0, 6144, 0.012288),
    ("MIXED", "1000000", 1000000.0, 6144, 0.006144),
    ("MIXED", "2000000", 2000000.0, 6144, 0.003072),
    ("MIXED", "5000000", 5000000.0, 6144, 0.0012288),
    ("MIXED", "10000000", 10000000.0, 6144, 0.0006144),
    ("MIXED", "20000000", 10000000.0, 6144, 0.0006144),
    ("MIXED", "40000000", 10000000.0, 6144, 0.0006144),

    ("LOGIC", "100", 2441.40625, 12288, 5.0331648),
    ("LOGIC", "500", 2441.40625, 12288, 5.0331648),
    ("LOGIC", "1000", 2441.40625, 12288, 5.0331648),
    ("LOGIC", "5000", 5000.0, 12288, 2.4576),
    ("LOGIC", "10000", 10000.0, 12288, 1.2288),
    ("LOGIC", "50000", 50000.0, 12288, 0.24576),
    ("LOGIC", "100000", 100000.0, 12288, 0.12288),
    ("LOGIC", "500000", 500000.0, 12288, 0.024576),
    ("LOGIC", "1000000", 1000000.0, 12288, 0.012288),
    ("LOGIC", "2000000", 2000000.0, 12288, 0.006144),
    ("LOGIC", "5000000", 5000000.0, 12288, 0.0024576),
    ("LOGIC", "10000000", 10000000.0, 12288, 0.0012288),
    ("LOGIC", "20000000", 20000000.0, 12288, 0.0006144),
    ("LOGIC", "40000000", 40000000.0, 12288, 0.0003072),
]

PROBED_LIMITS = {
    (mode, requested): {
        "mode": mode,
        "requested_rate_hz": requested,
        "actual_rate_hz": actual,
        "max_samples": max_samples,
        "max_time_s": max_time,
    }
    for mode, requested, actual, max_samples, max_time in PROBED_LIMIT_ROWS
}


def limit_rows_for_mode(mode: str):
    mode = mode.upper()
    return [row for row in PROBED_LIMIT_ROWS if row[0] == mode]


def rate_options_for_mode(mode: str) -> list[str]:
    return [requested for _, requested, _, _, _ in limit_rows_for_mode(mode)]


def probed_limit_for(mode: str, requested_rate_text: str) -> dict:
    mode = mode.upper()
    requested = str(requested_rate_text).strip()
    if (mode, requested) in PROBED_LIMITS:
        return PROBED_LIMITS[(mode, requested)]

    # Fallback for hand-typed values if the combobox is ever made editable.
    actual = get_actual_rate_hint(mode, requested)
    max_samples = MODE_CONFIGS.get(mode, {}).get("max_samples", 0)
    if not max_samples:
        # Avoid importing another constant from core; the measured table is authoritative.
        rows = limit_rows_for_mode(mode)
        max_samples = rows[0][3] if rows else 0
    max_time = max_samples / actual if actual > 0 else 0.0
    return {
        "mode": mode,
        "requested_rate_hz": requested,
        "actual_rate_hz": actual,
        "max_samples": max_samples,
        "max_time_s": max_time,
    }


def probed_actual_rate(mode: str, requested_rate_text: str) -> float:
    return float(probed_limit_for(mode, requested_rate_text)["actual_rate_hz"])


def fmt_time(seconds: float) -> str:
    if seconds >= 1:
        return f"{seconds:.6g} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.6g} ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:.6g} us"
    return f"{seconds * 1e9:.6g} ns"


def fmt_rate(rate: float) -> str:
    if rate >= 1e6:
        return f"{rate / 1e6:.6g} MHz"
    if rate >= 1e3:
        return f"{rate / 1e3:.6g} kHz"
    return f"{rate:.6g} Hz"


def trigger_sources_for_mode(mode: str) -> list[str]:
    """Return trigger-capable channel names for the selected BitScope mode."""
    cfg = MODE_CONFIGS.get(str(mode).upper(), {})
    return list(cfg.get("analog", {}).keys()) + list(cfg.get("digital", {}).keys())


def default_trigger_source_for_mode(mode: str) -> str:
    sources = trigger_sources_for_mode(mode)
    return sources[0] if sources else ""



# -----------------------------------------------------------------------------
# Planning / presets
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class CapturePlan:
    mode: str
    pv_recommended_source: str
    real_samples: int
    analog_output_samples: int
    logic_output_samples: int
    segment_count: int
    notes: str


def recommended_plan_for_mode(mode: str) -> CapturePlan:
    """User-friendly default plan for each BitScope mode."""
    mode = mode.upper()

    if mode == "FAST":
        return CapturePlan(
            mode=mode,
            pv_recommended_source="Segmented",
            real_samples=12288,
            analog_output_samples=14000,
            logic_output_samples=0,
            segment_count=1,
            notes="FAST maximum: 12288 real samples + hold-last padding to 14000, served as one block.",
        )

    if mode == "LOGIC":
        return CapturePlan(
            mode=mode,
            pv_recommended_source="Memory",
            real_samples=12288,
            analog_output_samples=0,
            logic_output_samples=14000,
            segment_count=1,
            notes="LOGIC maximum: 12288 real samples + hold-last padding to 14000. Validated with counter/edges.",
        )

    if mode == "DUAL":
        return CapturePlan(
            mode=mode,
            pv_recommended_source="Memory",
            real_samples=6144,
            analog_output_samples=7000,
            logic_output_samples=0,
            segment_count=1,
            notes="DUAL maximum: 6144 real samples + hold-last padding to 7000.",
        )

    if mode == "MIXED":
        return CapturePlan(
            mode=mode,
            pv_recommended_source="Memory",
            real_samples=6144,
            analog_output_samples=7000,
            logic_output_samples=14000,
            segment_count=1,
            notes="MIXED maximum: 6144 real samples; analog padded to 7000, logic padded to 14000 with hold-last.",
        )

    raise ValueError(f"Unsupported mode: {mode}")


def live_plan_for_mode(mode: str) -> CapturePlan:
    mode = mode.upper()
    has_analog = mode in ("FAST", "DUAL", "MIXED")
    has_logic = mode in ("LOGIC", "MIXED")

    return CapturePlan(
        mode=mode,
        pv_recommended_source="Live",
        real_samples=LIVE_SAMPLES,
        analog_output_samples=LIVE_SAMPLES if has_analog else 0,
        logic_output_samples=LIVE_SAMPLES if has_logic else 0,
        segment_count=1,
        notes="Universal Live mode: 1400 real samples, no padding.",
    )


def memory_plan_for_mode(mode: str) -> CapturePlan:
    mode = mode.upper()

    if mode == "FAST":
        return CapturePlan(
            mode=mode,
            pv_recommended_source="Memory",
            real_samples=ANALOG_MEMORY_SAMPLES,
            analog_output_samples=ANALOG_MEMORY_SAMPLES,
            logic_output_samples=0,
            segment_count=1,
            notes="Clean FAST Memory mode: 7000 real samples, no padding.",
        )

    if mode == "LOGIC":
        return recommended_plan_for_mode(mode)

    if mode == "DUAL":
        return recommended_plan_for_mode(mode)

    if mode == "MIXED":
        return recommended_plan_for_mode(mode)

    raise ValueError(f"Unsupported mode: {mode}")


def segmented_plan_for_mode(mode: str) -> CapturePlan:
    mode = mode.upper()

    if mode == "FAST":
        return recommended_plan_for_mode(mode)

    # For other modes segmented is not a good default. Use a single segment plan.
    base = memory_plan_for_mode(mode)
    return CapturePlan(
        mode=mode,
        pv_recommended_source="Segmented",
        real_samples=base.real_samples,
        analog_output_samples=base.analog_output_samples,
        logic_output_samples=base.logic_output_samples,
        segment_count=1,
        notes="Segmented is not recommended for this mode; served as one segment equivalent to Memory.",
    )


def plan_from_scpi_source(mode: str, waveform_mode: str, segmented: bool) -> CapturePlan:
    if segmented:
        return segmented_plan_for_mode(mode)
    if waveform_mode.upper() == "RAW":
        return memory_plan_for_mode(mode)
    return live_plan_for_mode(mode)


# -----------------------------------------------------------------------------
# Shared settings between GUI and SCPI server
# -----------------------------------------------------------------------------

@dataclass
class ProxySettings:
    mode: str = "LOGIC"
    rate_text: str = "1000000"
    analog_threshold: float = 1.5

    # Per-channel analog input and display scaling.
    # CH1 maps to ana0 / Rigol CHAN1. CH2 maps to ana1 / Rigol CHAN2.
    ch1_source_name: str = "BNC"
    ch1_range_label: str = "9.2 V"
    ch1_offset: float = 0.0
    ch1_probe_mode: str = "1x"
    ch1_probe_factor: float = 1.0
    ch1_yinc_mode: str = "auto"
    ch1_yinc: float = auto_yinc_for("9.2 V", 1.0)

    ch2_source_name: str = "BNC"
    ch2_range_label: str = "9.2 V"
    ch2_offset: float = 0.0
    ch2_probe_mode: str = "1x"
    ch2_probe_factor: float = 1.0
    ch2_yinc_mode: str = "auto"
    ch2_yinc: float = auto_yinc_for("9.2 V", 1.0)

    port: int = PORT
    use_simulator_data: bool = False  # Useful simulator mode if BitScope is not connected.

    # Simple BitLib hardware trigger. Forced uses BL_Trace(0.0, False).
    # Normal/Auto configure BL_Trigger() and use BL_Trace(timeout_s, False).
    trigger_mode: str = "Forced"
    trigger_source: str = "dig0"
    trigger_edge: str = "FALL"
    trigger_level: float = 1.5
    trigger_intro_s: float = 0.001
    trigger_delay_s: float = 0.0
    trigger_timeout_s: float = 0.5

    @property
    def actual_rate_hint(self) -> float:
        return probed_actual_rate(self.mode, self.rate_text)

    def analog_channel_settings(self) -> dict:
        # BitScope Micro + BitLib keep the analog source as POD even when BNC/X10
        # is requested. Keep the GUI POD/BNC selection as a physical connection
        # note only, and force BitLib source=POD for deterministic captures.
        return {
            "ana0": {
                "source": BL_SOURCE_OPTIONS["POD"],
                "range_index": range_index(self.ch1_range_label),
                "offset": self.ch1_offset,
                "probe_factor": self.ch1_probe_factor,
            },
            "ana1": {
                "source": BL_SOURCE_OPTIONS["POD"],
                "range_index": range_index(self.ch2_range_label),
                "offset": self.ch2_offset,
                "probe_factor": self.ch2_probe_factor,
            },
        }

    def trigger_config(self) -> dict | None:
        mode = str(self.trigger_mode).strip().lower()
        if mode in ("forced", "free-run", "freerun", "disabled", "none"):
            return {"enabled": False, "mode": "forced"}

        return {
            "enabled": True,
            "mode": mode,
            "source": self.trigger_source,
            "edge": str(self.trigger_edge).upper(),
            "level": float(self.trigger_level),
            "intro_s": float(self.trigger_intro_s),
            "delay_s": float(self.trigger_delay_s),
            "timeout_s": float(self.trigger_timeout_s),
        }

    def yinc_for_source(self, source: str) -> float:
        source = source.upper()
        if source == "CHAN2":
            return self.ch2_yinc
        return self.ch1_yinc


@dataclass
class ScpiState:
    source: str = "CHAN1"
    timebase: float = 1e-3
    horiz_offset: float = 0.0
    waveform_mode: str = "NORM"  # NORM=Live, RAW=Memory/Segmented
    segmented_mode: bool = False
    current_segment: int = 1
    memory_depth_request: int = LIVE_SAMPLES
    wave_points_request: int = LIVE_SAMPLES

    ch_disp: dict = field(default_factory=lambda: {1: True, 2: True})
    ch_scale: dict = field(default_factory=lambda: {1: 1.0, 2: 1.0})
    ch_offset: dict = field(default_factory=lambda: {1: 0.0, 2: 0.0})
    ch_probe: dict = field(default_factory=lambda: {1: 1.0, 2: 1.0})
    ch_coupling: dict = field(default_factory=lambda: {1: "DC", 2: "DC"})

    la_enabled: bool = True
    dig_disp: dict = field(default_factory=lambda: {i: True for i in range(16)})

    trigger_source: str = "CHAN1"
    trigger_slope: str = "POS"
    trigger_level: float = 0.0


@dataclass
class CaptureFrame:
    mode: str
    requested_rate_text: str
    actual_rate: float
    real_samples: int
    analog_output_samples: int
    logic_output_samples: int
    segment_count: int
    ch1: bytes
    ch2: bytes
    la: bytes
    ch1_yinc: float
    ch2_yinc: float
    timestamp: float
    plan_notes: str

    @property
    def real_time(self) -> float:
        return self.real_samples / self.actual_rate if self.actual_rate > 0 else 0.0


# -----------------------------------------------------------------------------
# Conversion helpers
# -----------------------------------------------------------------------------

def answer_ascii(text: str) -> bytes:
    return (text + "\n").encode("ascii")


def ieee_block(payload: bytes) -> bytes:
    length = str(len(payload)).encode("ascii")
    return b"#" + str(len(length)).encode("ascii") + length + payload + b"\n"


def normalize_cmd(cmd: str) -> str:
    u = cmd.strip().upper()
    if u and not u.startswith(("*", ":")):
        u = ":" + u
    return u


def parse_channel_num(cmd_upper: str, prefix: str) -> int | None:
    try:
        rest = cmd_upper[len(prefix):]
        digits = ""
        for c in rest:
            if c.isdigit():
                digits += c
            else:
                break
        return int(digits) if digits else None
    except Exception:
        return None


def set_bool_from_command(text_upper: str) -> bool:
    last = text_upper.split()[-1].strip()
    return last in ("1", "ON", "TRUE")


def clamp_byte(x: int) -> int:
    return max(0, min(255, int(x)))


def volts_to_rigol_byte(v: float, yinc: float) -> int:
    if yinc <= 0:
        yinc = ANALOG_YINC
    return clamp_byte(ANALOG_YREF + ANALOG_YOR + round(v / yinc))


def pad_list_hold_last(values, target_len, default_value=0):
    values = list(values)
    if len(values) >= target_len:
        return values[:target_len]
    pad_value = values[-1] if values else default_value
    return values + [pad_value] * (target_len - len(values))


def analog_to_rigol_bytes(values, output_len: int, yinc: float) -> bytes:
    padded = pad_list_hold_last(values, output_len, default_value=0.0)
    return bytes(volts_to_rigol_byte(v, yinc) for v in padded)


def pack_logic_words(digital: dict[str, list[int]], real_len: int, output_len: int) -> bytes:
    words = []

    for i in range(real_len):
        word = 0

        # Physical BitScope Micro digital inputs.
        for bit in range(6):
            name = f"dig{bit}"
            vals = digital.get(name)
            if vals is not None and i < len(vals) and vals[i]:
                word |= 1 << bit

        # Derived digital channels in MIXED.
        vals = digital.get("ana0_dig")
        if vals is not None and i < len(vals) and vals[i]:
            word |= 1 << 6

        vals = digital.get("ana1_dig")
        if vals is not None and i < len(vals) and vals[i]:
            word |= 1 << 7

        words.append(word & 0xFFFF)

    words = pad_list_hold_last(words, output_len, default_value=0)

    out = bytearray()
    for word in words:
        out.append(word & 0xFF)
        out.append((word >> 8) & 0xFF)
    return bytes(out)


# -----------------------------------------------------------------------------
# Capture manager
# -----------------------------------------------------------------------------

class CaptureManager:
    def __init__(self, settings: ProxySettings, log, status_callback=None):
        self.settings = settings
        self.log = log
        self.status_callback = status_callback
        self.lock = threading.Lock()
        self.frame: CaptureFrame | None = None
        self.dirty = True
        self.last_error: str | None = None

    def _status(self, kind: str, state: str, detail: str = ""):
        if self.status_callback:
            try:
                self.status_callback(kind, state, detail)
            except Exception:
                pass

    def mark_dirty(self, reason: str):
        with self.lock:
            self.dirty = True
            self.frame = None
        self.log(f"CAPTURE <= dirty: {reason}")

    def ensure_frame(self, plan: CapturePlan) -> CaptureFrame:
        with self.lock:
            current = self.frame
            needs_capture = (
                self.dirty
                or current is None
                or current.mode != self.settings.mode
                or current.requested_rate_text != self.settings.rate_text
                or current.real_samples != plan.real_samples
                or current.analog_output_samples != plan.analog_output_samples
                or current.logic_output_samples != plan.logic_output_samples
                or current.segment_count != plan.segment_count
            )

        if needs_capture:
            new_frame = self._capture(plan)
            with self.lock:
                self.frame = new_frame
                self.dirty = False
                self.last_error = None
            return new_frame

        return current

    def _capture(self, plan: CapturePlan) -> CaptureFrame:
        mode = self.settings.mode
        requested_rate = float(self.settings.rate_text)
        expected_actual_rate = self.settings.actual_rate_hint
        requested_time = plan.real_samples / expected_actual_rate if expected_actual_rate > 0 else 0.0

        self._status("capture", "capturing", "capturing / waiting for trigger")
        self._status("trigger", "waiting" if self.settings.trigger_config().get("enabled") else "forced", "")

        self.log("")
        self.log("=" * 72)
        self.log("BitScope acquisition")
        self.log(f"mode          = {mode}")
        self.log(f"requested Hz  = {self.settings.rate_text}")
        self.log(f"expected Hz   = {expected_actual_rate:g}")
        self.log(f"real samples  = {plan.real_samples}")
        self.log(f"requested time= {requested_time:.9g} s")
        self.log(f"plan          = {plan.notes}")
        self.log("=" * 72)

        if self.settings.use_simulator_data:
            frame = self._simulator_capture(plan)
            self._status("capture", "ready", f"SIMULATOR {frame.real_samples} samples")
            self._status("trigger", "simulator", "simulator")
            return frame

        try:
            result = capture_to_files(
                mode_name=mode,
                requested_rate=requested_rate,
                requested_time=requested_time,
                output_prefix=str(Path.cwd() / "_proxy_capture"),
                make_csv=False,
                make_sigrok_csv=False,
                make_svg=False,
                make_vcd=False,
                make_sr=False,
                analog_digital_threshold=self.settings.analog_threshold,
                trigger_config=self.settings.trigger_config(),
                analog_channel_settings=self.settings.analog_channel_settings(),
                log=self.log,
            )
        except Exception as e:
            self.last_error = str(e)
            self._status("capture", "error", str(e))
            self._status("trigger", "unknown", "capture error")
            self.log("ERROR capturing BitScope:")
            self.log(str(e))
            self.log(traceback.format_exc())
            raise

        analog = result.get("analog", {})
        digital = result.get("digital", {})
        actual_rate = float(result.get("actual_rate", requested_rate))
        trigger_result = result.get("trigger")
        if trigger_result:
            trigger_status = str(trigger_result.get("trigger_status") or "unknown")
            trigger_detail = (
                f"{trigger_result.get('source')} {trigger_result.get('edge')} "
                f"idx={trigger_result.get('event_index')} "
                f"elapsed={trigger_result.get('elapsed_s')}"
            )
            self._status("trigger", trigger_status, trigger_detail)
            self.log(
                "Proxy trigger     = "
                f"{trigger_status} "
                f"source={trigger_result.get('source')} "
                f"edge={trigger_result.get('edge')} "
                f"event_index={trigger_result.get('event_index')} "
                f"elapsed={trigger_result.get('elapsed_s')}"
            )
        else:
            self._status("trigger", "forced", "")

        real_n = 0
        for d in (analog, digital):
            for values in d.values():
                real_n = max(real_n, len(values))
        real_n = min(real_n, plan.real_samples) if real_n else plan.real_samples

        ch1 = b""
        ch2 = b""
        la = b""

        if plan.analog_output_samples:
            ch1_values = analog.get("ana0", [])[:real_n]
            ch2_values = analog.get("ana1", [])[:real_n]

            if mode == "FAST" and not ch2_values:
                ch2_values = []

            ch1 = analog_to_rigol_bytes(ch1_values, plan.analog_output_samples, self.settings.ch1_yinc)
            ch2 = analog_to_rigol_bytes(ch2_values, plan.analog_output_samples, self.settings.ch2_yinc)

        if plan.logic_output_samples:
            la = pack_logic_words(digital, real_n, plan.logic_output_samples)

        self.log(f"Proxy actual_rate   = {actual_rate:g} Hz")
        self.log(f"Proxy real samples  = {real_n}")
        self.log(f"Proxy real time     = {real_n / actual_rate:.9g} s")
        self.log(f"Proxy CH1 bytes     = {len(ch1)}")
        self.log(f"Proxy CH2 bytes     = {len(ch2)}")
        self.log(f"Proxy LA bytes      = {len(la)}")
        self._status("capture", "ready", f"{mode} @ {actual_rate:g} Hz, {real_n} samples")

        return CaptureFrame(
            mode=mode,
            requested_rate_text=self.settings.rate_text,
            actual_rate=actual_rate,
            real_samples=real_n,
            analog_output_samples=plan.analog_output_samples,
            logic_output_samples=plan.logic_output_samples,
            segment_count=plan.segment_count,
            ch1=ch1,
            ch2=ch2,
            la=la,
            ch1_yinc=self.settings.ch1_yinc,
            ch2_yinc=self.settings.ch2_yinc,
            timestamp=time.time(),
            plan_notes=plan.notes,
        )

    def _simulator_capture(self, plan: CapturePlan) -> CaptureFrame:
        # Simulator fallback for testing the proxy without a BitScope connected.
        rate = self.settings.actual_rate_hint
        yinc1 = self.settings.ch1_yinc
        yinc2 = self.settings.ch2_yinc

        analog_len = plan.analog_output_samples
        logic_len = plan.logic_output_samples
        real_n = plan.real_samples

        ch1_vals = [i / 1000.0 for i in range(real_n)]
        ch2_vals = [1.0 for _ in range(real_n)]

        ch1 = analog_to_rigol_bytes(ch1_vals, analog_len, yinc1) if analog_len else b""
        ch2 = analog_to_rigol_bytes(ch2_vals, analog_len, yinc2) if analog_len else b""

        digital = {
            "dig0": [(i & 1) for i in range(real_n)],
            "dig1": [((i >> 1) & 1) for i in range(real_n)],
            "dig2": [((i >> 2) & 1) for i in range(real_n)],
            "dig3": [((i >> 3) & 1) for i in range(real_n)],
            "dig4": [((i >> 4) & 1) for i in range(real_n)],
            "dig5": [((i >> 5) & 1) for i in range(real_n)],
        }
        la = pack_logic_words(digital, real_n, logic_len) if logic_len else b""

        return CaptureFrame(
            mode=self.settings.mode,
            requested_rate_text=self.settings.rate_text,
            actual_rate=rate,
            real_samples=real_n,
            analog_output_samples=analog_len,
            logic_output_samples=logic_len,
            segment_count=plan.segment_count,
            ch1=ch1,
            ch2=ch2,
            la=la,
            ch1_yinc=self.settings.ch1_yinc,
            ch2_yinc=self.settings.ch2_yinc,
            timestamp=time.time(),
            plan_notes=plan.notes + " [SIMULADOR]",
        )


# -----------------------------------------------------------------------------
# SCPI server
# -----------------------------------------------------------------------------

class RigolScpiServer:
    def __init__(self, settings: ProxySettings, log, scpi_log=None, status_callback=None):
        self.settings = settings
        self.log = log
        self.scpi_log = scpi_log or log
        self.status_callback = status_callback
        self.scpi = ScpiState()
        self.capture = CaptureManager(settings, log, status_callback=status_callback)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sock: socket.socket | None = None
        self.read_offsets = {"CHAN1": 0, "CHAN2": 0, "LA": 0}
        self.client_lock = threading.Lock()
        self.active_clients = 0

    def _status(self, kind: str, state, detail: str = ""):
        if self.status_callback:
            try:
                self.status_callback(kind, state, detail)
            except Exception:
                pass

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self._status("clients", 0, "")
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._status("clients", 0, "")
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.capture.mark_dirty("server stop")

    def _serve(self):
        self.log(f"SCPI server listening on {HOST}:{self.settings.port}")
        self.log(DEFAULT_PULSEVIEW_CMD)

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                self.sock = s
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((HOST, int(self.settings.port)))
                s.listen(5)
                s.settimeout(0.5)

                while not self.stop_event.is_set():
                    try:
                        conn, addr = s.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break

                    threading.Thread(target=self._client_thread, args=(conn, addr), daemon=True).start()
        except Exception as e:
            self.log(f"SCPI server error: {e}")
            self.log(traceback.format_exc())
        finally:
            self.log("SCPI server stopped")

    def _client_thread(self, conn: socket.socket, addr):
        with self.client_lock:
            self.active_clients += 1
            active = self.active_clients
        self._status("clients", active, str(addr))
        self.log(f"PulseView client connected: {addr} (clients={active})")
        self.scpi_log(f"Client connected: {addr}")
        buf = b""
        try:
            with conn:
                while not self.stop_event.is_set():
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("ascii", errors="replace").strip()
                        for part in [p.strip() for p in text.split(";") if p.strip()]:
                            resp = self.handle_command(part)
                            if resp is not None:
                                conn.sendall(resp)
        except ConnectionResetError:
            pass
        except Exception as e:
            self.log(f"Client error {addr}: {e}")
            self.log(traceback.format_exc())
        finally:
            with self.client_lock:
                self.active_clients = max(0, self.active_clients - 1)
                active = self.active_clients
            self._status("clients", active, str(addr))
            self.log(f"PulseView client disconnected: {addr} (clients={active})")
            self.scpi_log(f"Client disconnected: {addr}")

    def current_plan(self) -> CapturePlan:
        return plan_from_scpi_source(
            self.settings.mode,
            self.scpi.waveform_mode,
            self.scpi.segmented_mode,
        )


    def _rate_for_metadata(self) -> float:
        """Rate to use for SCPI metadata before/after capture.

        If a fresh frame exists, use the frame actual_rate. If settings changed
        and the frame is dirty/cleared, use the current GUI-selected measured
        rate from the table. This avoids returning stale timing metadata to
        PulseView on the first capture after a rate change.
        """
        try:
            with self.capture.lock:
                frame = self.capture.frame
                dirty = self.capture.dirty
            if frame is not None and not dirty:
                return float(frame.actual_rate)
        except Exception:
            pass
        return float(self.settings.actual_rate_hint)

    def _display_samples_for_metadata(self) -> int:
        """Number of samples PulseView should consider for current source/plan."""
        try:
            plan = self.current_plan()
            src = self.scpi.source.upper()
            if src == "LA" and plan.logic_output_samples:
                return int(plan.logic_output_samples)
            if src in ("CHAN1", "CHAN2") and plan.analog_output_samples:
                return int(plan.analog_output_samples)
            return int(max(plan.analog_output_samples, plan.logic_output_samples, plan.real_samples, LIVE_SAMPLES))
        except Exception:
            return LIVE_SAMPLES

    def _timebase_for_metadata(self) -> float:
        """Dynamic TIM:SCAL value consistent with current proxy rate.

        Rigol scopes use seconds/division. The driver sometimes queries
        TIM:SCAL as well as WAV:XINC. Returning this dynamically prevents
        PulseView/libsigrok from keeping the previous horizontal scale for
        the first capture after changing rate in the proxy GUI.
        """
        rate = self._rate_for_metadata()
        samples = max(1, self._display_samples_for_metadata())
        if rate <= 0:
            return self.scpi.timebase
        return samples / rate / 12.0

    def reset_offset(self):
        src = self.scpi.source.upper()
        self.read_offsets[src] = 0
        self.scpi_log(f"READ <= reset {src}")

    def source_payload(self, frame: CaptureFrame, source: str) -> bytes:
        source = source.upper()

        if source == "CHAN1":
            payload = frame.ch1
        elif source == "CHAN2":
            payload = frame.ch2
        elif source == "LA":
            payload = frame.la
        else:
            payload = b""

        # Robustness: if PulseView asks a channel that is not present in the
        # selected BitScope mode, return a flat/inert signal with the length
        # that rigol-ds expects. This avoids hanging if the user forgot to
        # disable CH2 or LA in PulseView.
        if not payload and source in ("CHAN1", "CHAN2"):
            analog_len = frame.analog_output_samples or (ANALOG_MEMORY_SAMPLES if self.scpi.waveform_mode == "RAW" else LIVE_SAMPLES)
            payload = bytes([ANALOG_YREF]) * analog_len

        if not payload and source == "LA":
            if self.scpi.waveform_mode == "RAW" or self.scpi.segmented_mode:
                byte_len = max(self.scpi.wave_points_request, LOGIC_MEMORY_SAMPLES * 2)
            else:
                byte_len = LIVE_SAMPLES * 2
            payload = bytes(byte_len)

        # Segmented mode slices per segment.
        if self.scpi.segmented_mode and frame.segment_count > 1 and source in ("CHAN1", "CHAN2"):
            seg = max(1, min(frame.segment_count, self.scpi.current_segment))
            start = (seg - 1) * SEGMENT_SAMPLES
            end = seg * SEGMENT_SAMPLES
            payload = payload[start:end]

        return payload

    def waveform_stat(self) -> bytes:
        plan = self.current_plan()
        frame = self.capture.ensure_frame(plan)
        src = self.scpi.source.upper()
        payload = self.source_payload(frame, src)
        remaining = max(0, len(payload) - self.read_offsets.get(src, 0))
        return answer_ascii(f"IDLE,{remaining}")

    def waveform_data(self) -> bytes:
        plan = self.current_plan()
        frame = self.capture.ensure_frame(plan)
        src = self.scpi.source.upper()
        payload_all = self.source_payload(frame, src)

        if src == "LA":
            chunk_len = LOGIC_CHUNK_BYTES
        else:
            chunk_len = ANALOG_CHUNK_BYTES

        offset = self.read_offsets.get(src, 0)
        payload = payload_all[offset:offset + chunk_len]
        self.read_offsets[src] = offset + len(payload)
        remaining = max(0, len(payload_all) - self.read_offsets[src])

        self.scpi_log(
            f"SCPI => <{src} offset={offset} payload={len(payload)} remaining={remaining}>"
        )

        return ieee_block(payload)

    def handle_command(self, raw: str) -> bytes | None:
        cmd = raw.strip()
        if not cmd:
            return None
        u = normalize_cmd(cmd)

        if u != ":TRIG:STAT?":
            self.scpi_log(f"SCPI <= {cmd}")

        # Identification.
        if u == "*IDN?":
            return answer_ascii("Rigol Technologies,MSO2072A,BSMICRO0001,00.02.05.00.01")
        if u == "*OPC?":
            return answer_ascii("1")
        if u == "*ESR?":
            return answer_ascii("0")
        if u in ("*CLS", "*RST"):
            if u == "*RST":
                self.capture.mark_dirty("*RST")
            return None

        # Run/stop.
        if u in (":RUN", "RUN", ":SING", ":SINGL", ":SINGLE", "SING", "SINGL", "SINGLE"):
            self.log(f"SCPI event <= {cmd}")
            self.capture.mark_dirty(u)
            return None
        if u in (":STOP", "STOP"):
            self.log(f"SCPI event <= {cmd}")
            return None
        if u == ":TRIG:STAT?":
            if self.scpi.waveform_mode == "RAW" or self.scpi.segmented_mode:
                return answer_ascii("STOP")
            return answer_ascii("TD")

        # Segmented waveform replay.
        if u == ":FUNC:WREP:FMAX?":
            self.scpi.segmented_mode = True
            seg_count = segmented_plan_for_mode(self.settings.mode).segment_count
            self.log(f"STATE <= segmented_mode=True, FMAX={seg_count}")
            return answer_ascii(str(seg_count))
        if u == ":FUNC:WREP:FCUR?":
            return answer_ascii(str(self.scpi.current_segment))
        if u.startswith(":FUNC:WREP:FCUR "):
            try:
                self.scpi.current_segment = int(cmd.split()[-1])
                self.read_offsets = {"CHAN1": 0, "CHAN2": 0, "LA": 0}
                self.log(f"STATE <= current_segment={self.scpi.current_segment}")
            except Exception:
                pass
            return None
        if u.startswith(":FUNC:WREP"):
            return None

        # Channels.
        if u.startswith(":CHAN") and ":DISP?" in u:
            n = parse_channel_num(u, ":CHAN")
            return answer_ascii("1" if self.scpi.ch_disp.get(n, False) else "0")
        if u.startswith(":CHAN") and ":DISP " in u:
            n = parse_channel_num(u, ":CHAN")
            if n is not None:
                self.scpi.ch_disp[n] = set_bool_from_command(u)
            return None
        if u.startswith(":CHAN") and ":PROB?" in u:
            n = parse_channel_num(u, ":CHAN")
            return answer_ascii(str(self.scpi.ch_probe.get(n, 1.0)))
        if u.startswith(":CHAN") and ":PROB " in u:
            return None
        if u.startswith(":CHAN") and ":SCAL?" in u:
            n = parse_channel_num(u, ":CHAN")
            return answer_ascii(str(self.scpi.ch_scale.get(n, 1.0)))
        if u.startswith(":CHAN") and ":SCAL " in u:
            return None
        if u.startswith(":CHAN") and ":OFFS?" in u:
            n = parse_channel_num(u, ":CHAN")
            return answer_ascii(str(self.scpi.ch_offset.get(n, 0.0)))
        if u.startswith(":CHAN") and ":OFFS " in u:
            return None
        if u.startswith(":CHAN") and ":COUP?" in u:
            n = parse_channel_num(u, ":CHAN")
            return answer_ascii(self.scpi.ch_coupling.get(n, "DC"))
        if u.startswith(":CHAN") and ":COUP " in u:
            return None

        # Logic analyzer channels.
        if u == ":LA:STAT?":
            return answer_ascii("1" if self.scpi.la_enabled else "0")
        if u.startswith(":LA:STAT "):
            self.scpi.la_enabled = set_bool_from_command(u)
            return None
        if u.startswith(":LA:DIG") and ":DISP?" in u:
            try:
                n = int(u.split(":DIG", 1)[1].split(":")[0])
                return answer_ascii("1" if self.scpi.dig_disp.get(n, False) else "0")
            except Exception:
                return answer_ascii("0")
        if u.startswith(":LA:DIG") and ":DISP " in u:
            try:
                n = int(u.split(":DIG", 1)[1].split(":")[0])
                self.scpi.dig_disp[n] = set_bool_from_command(u)
            except Exception:
                pass
            return None
        if u.startswith(":LA:DIG") and ":THR?" in u:
            return answer_ascii("1.4")
        if u.startswith(":LA:DIG") and ":THR " in u:
            return None

        # Timebase and trigger.
        if u == ":TIM:SCAL?":
            timebase = self._timebase_for_metadata()
            msg = (
                f"SCPI => TIM:SCAL? {timebase:.12g}  "
                f"rate={self._rate_for_metadata():g} samples={self._display_samples_for_metadata()}"
            )
            self.log(msg)
            self.scpi_log(msg)
            return answer_ascii(f"{timebase:.12g}")
        if u.startswith(":TIM:SCAL "):
            try:
                # Keep the user/PulseView requested value for diagnostics, but
                # the proxy owns timing metadata and returns dynamic TIM:SCAL?
                # from the selected BitScope rate.
                self.scpi.timebase = float(cmd.split()[-1])
            except Exception:
                pass
            return None
        if u == ":TIM:OFFS?":
            return answer_ascii(str(self.scpi.horiz_offset))
        if u.startswith(":TIM:OFFS "):
            return None
        if u == ":TRIG:EDGE:SOUR?":
            return answer_ascii(self.scpi.trigger_source)
        if u.startswith(":TRIG:EDGE:SOUR "):
            self.scpi.trigger_source = cmd.split()[-1].upper()
            return None
        if u == ":TRIG:EDGE:SLOP?":
            return answer_ascii(self.scpi.trigger_slope)
        if u.startswith(":TRIG:EDGE:SLOP "):
            self.scpi.trigger_slope = cmd.split()[-1].upper()
            return None
        if u == ":TRIG:EDGE:LEV?":
            return answer_ascii(str(self.scpi.trigger_level))
        if u.startswith(":TRIG:EDGE:LEV "):
            return None
        if u == ":TRIG:MODE?":
            return answer_ascii("EDGE")
        if u.startswith(":TRIG:MODE "):
            return None

        # Acquisition.
        if u in (":ACQ:MDEP?", "ACQ:MDEP?"):
            return answer_ascii(str(self.scpi.memory_depth_request))
        if u.startswith(":ACQ:MDEP") or u.startswith("ACQ:MDEP"):
            self.log(f"SCPI acquisition <= {cmd}")
            try:
                self.scpi.memory_depth_request = int(float(cmd.split()[-1]))
            except Exception:
                pass
            return None
        if u == ":ACQ:TYPE?":
            return answer_ascii("NORM")
        if u.startswith(":ACQ:TYPE "):
            return None

        # Waveform.
        if u.startswith(":WAV:SOUR "):
            self.scpi.source = cmd.split()[-1].upper()
            return None
        if u == ":WAV:SOUR?":
            return answer_ascii(self.scpi.source)
        if u == ":WAV:YINC?":
            # YINC is per analog channel. PulseView asks after setting WAV:SOUR.
            try:
                frame = self.capture.frame
                if frame and self.scpi.source.upper() == "CHAN2":
                    return answer_ascii(str(frame.ch2_yinc))
                if frame:
                    return answer_ascii(str(frame.ch1_yinc))
            except Exception:
                pass
            return answer_ascii(str(self.settings.yinc_for_source(self.scpi.source)))
        if u == ":WAV:YOR?":
            return answer_ascii(str(ANALOG_YOR))
        if u == ":WAV:YREF?":
            return answer_ascii(str(ANALOG_YREF))
        if u == ":WAV:XINC?":
            actual_rate = self._rate_for_metadata()
            xinc = 1.0 / actual_rate
            msg = f"SCPI => WAV:XINC? {xinc:.12g}  actual_rate={actual_rate:g}"
            self.log(msg)
            self.scpi_log(msg)
            return answer_ascii(f"{xinc:.12g}")
        if u == ":WAV:XOR?":
            return answer_ascii("0")
        if u == ":WAV:XREF?":
            return answer_ascii("0")
        if u == ":WAV:STAT?":
            return self.waveform_stat()
        if u.startswith(":WAV:POIN "):
            try:
                self.scpi.wave_points_request = int(float(cmd.split()[-1]))
            except Exception:
                pass
            return None
        if u == ":WAV:POIN?":
            plan = self.current_plan()
            if self.scpi.source.upper() == "LA":
                return answer_ascii(str(plan.logic_output_samples * 2))
            return answer_ascii(str(plan.analog_output_samples))
        if u.startswith(":WAV:FORM "):
            return None
        if u == ":WAV:FORM?":
            return answer_ascii("BYTE")
        if u.startswith(":WAV:MODE "):
            self.scpi.waveform_mode = cmd.split()[-1].upper()
            # If PulseView explicitly asks for NORM, it is live, not segmented.
            if self.scpi.waveform_mode == "NORM":
                self.scpi.segmented_mode = False
            return None
        if u == ":WAV:MODE?":
            return answer_ascii(self.scpi.waveform_mode)
        if u == ":WAV:RES":
            self.reset_offset()
            return None
        if u == ":WAV:BEG":
            return None
        if u == ":WAV:END":
            self.scpi_log(f"READ <= WAV:END for {self.scpi.source}")
            return None
        if u == ":WAV:DATA?":
            return self.waveform_data()

        if u.endswith("?"):
            self.scpi_log(f"SCPI ?? unknown query, replying 0: {cmd}")
            return answer_ascii("0")

        self.scpi_log(f"SCPI -- unknown command ignored: {cmd}")
        return None


# -----------------------------------------------------------------------------
# GUI
# -----------------------------------------------------------------------------

class ProxyGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BitScope Micro Rigol/MSO proxy")

        self.log_queue = queue.Queue()
        self.scpi_log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.settings = ProxySettings()
        self.server: RigolScpiServer | None = None
        self.client_count = 0

        self.mode_var = tk.StringVar(value=self.settings.mode)
        self.rate_var = tk.StringVar(value=self.settings.rate_text)
        self.threshold_var = tk.StringVar(value=str(self.settings.analog_threshold))

        self.ch1_source_var = tk.StringVar(value=self.settings.ch1_source_name)
        self.ch1_range_var = tk.StringVar(value=self.settings.ch1_range_label)
        self.ch1_offset_var = tk.StringVar(value=str(self.settings.ch1_offset))
        self.ch1_probe_mode_var = tk.StringVar(value=self.settings.ch1_probe_mode)
        self.ch1_probe_factor_var = tk.StringVar(value=str(self.settings.ch1_probe_factor))
        self.ch1_yinc_mode_var = tk.StringVar(value=self.settings.ch1_yinc_mode)
        self.ch1_yinc_var = tk.StringVar(value=str(self.settings.ch1_yinc))

        self.ch2_source_var = tk.StringVar(value=self.settings.ch2_source_name)
        self.ch2_range_var = tk.StringVar(value=self.settings.ch2_range_label)
        self.ch2_offset_var = tk.StringVar(value=str(self.settings.ch2_offset))
        self.ch2_probe_mode_var = tk.StringVar(value=self.settings.ch2_probe_mode)
        self.ch2_probe_factor_var = tk.StringVar(value=str(self.settings.ch2_probe_factor))
        self.ch2_yinc_mode_var = tk.StringVar(value=self.settings.ch2_yinc_mode)
        self.ch2_yinc_var = tk.StringVar(value=str(self.settings.ch2_yinc))

        self.simulator_var = tk.BooleanVar(value=False)

        self.trigger_mode_var = tk.StringVar(value=self.settings.trigger_mode)
        self.trigger_source_var = tk.StringVar(value=self.settings.trigger_source)
        self.trigger_edge_var = tk.StringVar(value=self.settings.trigger_edge)
        self.trigger_level_var = tk.StringVar(value=str(self.settings.trigger_level))
        self.trigger_intro_var = tk.StringVar(value=str(self.settings.trigger_intro_s))
        self.trigger_delay_var = tk.StringVar(value=str(self.settings.trigger_delay_s))
        self.trigger_timeout_var = tk.StringVar(value=str(self.settings.trigger_timeout_s))

        self.export_prefix_var = tk.StringVar(value=str(Path.cwd() / "captures" / "capture"))
        self.export_timestamp_var = tk.BooleanVar(value=True)
        self.export_csv_var = tk.BooleanVar(value=False)
        self.export_sigrok_csv_var = tk.BooleanVar(value=False)
        self.export_svg_var = tk.BooleanVar(value=False)
        self.export_vcd_var = tk.BooleanVar(value=False)
        self.export_sr_var = tk.BooleanVar(value=True)
        self.export_open_pv_var = tk.BooleanVar(value=False)
        self.export_worker = None

        self.status_var = tk.StringVar(value="BitScope")
        self.server_status_var = tk.StringVar(value="Proxy stopped")
        self.python_status_var = tk.StringVar(value=f"Python {python_bits()}-bit")
        self.capture_status_var = tk.StringVar(value="● Ready")
        self.trigger_status_var = tk.StringVar(value="● Forced")
        self.last_capture_var = tk.StringVar(value="Last: —")

        self._settings_load_message = None
        self._load_gui_settings()

        self._build_ui()
        self._set_capture_indicator("ready", "")
        self._set_trigger_indicator("forced", "")
        self._on_mode_changed()
        self._update_status_labels()
        self._update_plan_text()
        if self._settings_load_message:
            self._log(self._settings_load_message)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_log_queue()

    def _collect_gui_settings(self) -> dict:
        """Return GUI settings as JSON-serialisable values.

        This intentionally stores widget values, not server runtime state.
        If a future version adds/removes keys, unknown keys are simply ignored
        on load and missing keys keep defaults.
        """
        return {
            "version": 1,
            "mode": self.mode_var.get(),
            "rate_text": self.rate_var.get(),
            "analog_threshold": self.threshold_var.get(),
            "simulator": bool(self.simulator_var.get()),
            "fake_data": bool(self.simulator_var.get()),  # backward compatibility

            "ch1": {
                "source": self.ch1_source_var.get(),
                "range": self.ch1_range_var.get(),
                "offset": self.ch1_offset_var.get(),
                "probe_mode": self.ch1_probe_mode_var.get(),
                "probe_factor": self.ch1_probe_factor_var.get(),
                "yinc_mode": self.ch1_yinc_mode_var.get(),
                "yinc": self.ch1_yinc_var.get(),
            },
            "ch2": {
                "source": self.ch2_source_var.get(),
                "range": self.ch2_range_var.get(),
                "offset": self.ch2_offset_var.get(),
                "probe_mode": self.ch2_probe_mode_var.get(),
                "probe_factor": self.ch2_probe_factor_var.get(),
                "yinc_mode": self.ch2_yinc_mode_var.get(),
                "yinc": self.ch2_yinc_var.get(),
            },

            "trigger": {
                "mode": self.trigger_mode_var.get(),
                "source": self.trigger_source_var.get(),
                "edge": self.trigger_edge_var.get(),
                "level": self.trigger_level_var.get(),
                "intro_s": self.trigger_intro_var.get(),
                "delay_s": self.trigger_delay_var.get(),
                "timeout_s": self.trigger_timeout_var.get(),
            },

            "export": {
                "prefix": self.export_prefix_var.get(),
                "timestamp": bool(self.export_timestamp_var.get()),
                "csv": bool(self.export_csv_var.get()),
                "sigrok_csv": bool(self.export_sigrok_csv_var.get()),
                "svg": bool(self.export_svg_var.get()),
                "vcd": bool(self.export_vcd_var.get()),
                "sr": bool(self.export_sr_var.get()),
                "open_pulseview": bool(self.export_open_pv_var.get()),
            },
        }

    def _save_gui_settings(self):
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self._collect_gui_settings(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._log(f"Settings saved: {SETTINGS_FILE}")
        except Exception as e:
            self._log(f"WARNING: could not save settings JSON: {e}")

    def _load_gui_settings(self):
        if not SETTINGS_FILE.exists():
            return

        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            self._settings_load_message = f"WARNING: could not load settings JSON: {e}"
            return

        try:
            mode = str(data.get("mode", self.mode_var.get())).upper()
            if mode in MODE_CONFIGS:
                self.mode_var.set(mode)

            rate = str(data.get("rate_text", self.rate_var.get()))
            # If the rate is invalid for the loaded mode, _on_mode_changed() will
            # replace it with a valid default after widgets are built.
            self.rate_var.set(rate)

            if "analog_threshold" in data:
                self.threshold_var.set(str(data["analog_threshold"]))

            def load_channel(prefix: str, ch: dict):
                if not isinstance(ch, dict):
                    return
                if prefix == "ch1":
                    source_var = self.ch1_source_var
                    range_var = self.ch1_range_var
                    offset_var = self.ch1_offset_var
                    probe_mode_var = self.ch1_probe_mode_var
                    probe_factor_var = self.ch1_probe_factor_var
                    yinc_mode_var = self.ch1_yinc_mode_var
                    yinc_var = self.ch1_yinc_var
                else:
                    source_var = self.ch2_source_var
                    range_var = self.ch2_range_var
                    offset_var = self.ch2_offset_var
                    probe_mode_var = self.ch2_probe_mode_var
                    probe_factor_var = self.ch2_probe_factor_var
                    yinc_mode_var = self.ch2_yinc_mode_var
                    yinc_var = self.ch2_yinc_var

                source = ch.get("source")
                if source in GUI_ANALOG_SOURCE_OPTIONS:
                    source_var.set(source)
                rng = ch.get("range")
                if rng in BL_RANGE_OPTIONS:
                    range_var.set(rng)
                if "offset" in ch:
                    offset_var.set(str(ch["offset"]))
                probe_mode = ch.get("probe_mode")
                if probe_mode in GUI_PROBE_OPTIONS:
                    probe_mode_var.set(probe_mode)
                if "probe_factor" in ch:
                    probe_factor_var.set(str(ch["probe_factor"]))
                yinc_mode = ch.get("yinc_mode")
                if yinc_mode in GUI_YINC_MODE_OPTIONS:
                    yinc_mode_var.set(yinc_mode)
                if "yinc" in ch:
                    yinc_var.set(str(ch["yinc"]))

            load_channel("ch1", data.get("ch1", {}))
            load_channel("ch2", data.get("ch2", {}))

            self.simulator_var.set(bool(data.get("simulator", data.get("fake_data", self.simulator_var.get()))))

            trigger = data.get("trigger", {})
            if isinstance(trigger, dict):
                trig_mode = trigger.get("mode")
                if trig_mode in GUI_TRIGGER_MODE_OPTIONS:
                    self.trigger_mode_var.set(trig_mode)
                trig_source = trigger.get("source")
                if isinstance(trig_source, str) and trig_source:
                    self.trigger_source_var.set(trig_source)
                trig_edge = trigger.get("edge")
                if trig_edge in GUI_TRIGGER_EDGE_OPTIONS:
                    self.trigger_edge_var.set(trig_edge)
                if "level" in trigger:
                    self.trigger_level_var.set(str(trigger["level"]))
                if "intro_s" in trigger:
                    self.trigger_intro_var.set(str(trigger["intro_s"]))
                if "delay_s" in trigger:
                    self.trigger_delay_var.set(str(trigger["delay_s"]))
                if "timeout_s" in trigger:
                    self.trigger_timeout_var.set(str(trigger["timeout_s"]))

            export = data.get("export", {})
            if isinstance(export, dict):
                if "prefix" in export:
                    self.export_prefix_var.set(str(export["prefix"]))
                if "timestamp" in export:
                    self.export_timestamp_var.set(bool(export["timestamp"]))
                if "csv" in export:
                    self.export_csv_var.set(bool(export["csv"]))
                if "sigrok_csv" in export:
                    self.export_sigrok_csv_var.set(bool(export["sigrok_csv"]))
                if "svg" in export:
                    self.export_svg_var.set(bool(export["svg"]))
                if "vcd" in export:
                    self.export_vcd_var.set(bool(export["vcd"]))
                if "sr" in export:
                    self.export_sr_var.set(bool(export["sr"]))
                if "open_pulseview" in export:
                    self.export_open_pv_var.set(bool(export["open_pulseview"]))

            self._settings_load_message = f"Settings loaded: {SETTINGS_FILE}"
        except Exception as e:
            self._settings_load_message = f"WARNING: invalid settings JSON ignored: {e}"

    def _on_close(self):
        self._save_gui_settings()
        if self.server:
            self.server.stop()
            self.server = None
        self.root.destroy()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        # ------------------------------------------------------------
        # 1) Top: status + proxy actions. This is the normal workflow:
        #    start proxy, open PulseView, apply settings, recapture.
        # ------------------------------------------------------------
        row = 0
        top = ttk.LabelFrame(main, text="Proxy / PulseView", padding=8)
        top.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(10, weight=1)

        self.status_label = ttk.Label(top, textvariable=self.status_var, font=("Segoe UI", 10, "bold"))
        self.status_label.grid(row=0, column=0, sticky="w")

        ttk.Label(top, text="   |   ").grid(row=0, column=1, sticky="w")
        self.server_status_label = ttk.Label(top, textvariable=self.server_status_var)
        self.server_status_label.grid(row=0, column=2, sticky="w")
        ttk.Label(top, text="   |   ").grid(row=0, column=3, sticky="w")
        ttk.Label(top, textvariable=self.python_status_var).grid(row=0, column=4, sticky="w")

        self.start_button = ttk.Button(top, text="Start proxy", command=self._start_server)
        self.start_button.grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.stop_button = ttk.Button(top, text="Stop proxy", command=self._stop_server, state="disabled")
        self.stop_button.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Button(top, text="Open PulseView", command=self._open_pulseview).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(top, text="Copy PV command", command=self._copy_pulseview_command).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(top, text="Apply settings", command=self._apply_settings).grid(row=1, column=4, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(top, text="Mark recapture", command=self._mark_recapture).grid(row=1, column=5, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Button(top, text="Clear logs", command=self._clear_log).grid(row=1, column=6, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Checkbutton(
            top,
            text="Simulator",
            variable=self.simulator_var,
            command=self._auto_apply_settings,
        ).grid(row=1, column=7, sticky="w", padx=(18, 0), pady=(8, 0))

        # ------------------------------------------------------------
        # 2) Export to file. Kept high because it is a separate workflow.
        # ------------------------------------------------------------
        row += 1
        export = ttk.LabelFrame(main, text="Export capture to file", padding=8)
        export.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        export.columnconfigure(1, weight=1)

        ttk.Label(export, text="Base name").grid(row=0, column=0, sticky="w")
        ttk.Entry(export, textvariable=self.export_prefix_var, width=70).grid(row=0, column=1, columnspan=5, sticky="ew", padx=(6, 0))
        ttk.Button(export, text="Browse...", command=self._choose_export_prefix).grid(row=0, column=6, sticky="w", padx=(8, 0))
        self.export_button = ttk.Button(export, text="Capture to file", command=self._start_export_capture)
        self.export_button.grid(row=0, column=7, sticky="w", padx=(8, 0))

        ttk.Checkbutton(export, text="Timestamp", variable=self.export_timestamp_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="CSV", variable=self.export_csv_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="sigrok CSV", variable=self.export_sigrok_csv_var).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="SVG", variable=self.export_svg_var).grid(row=1, column=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="VCD", variable=self.export_vcd_var).grid(row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="SR", variable=self.export_sr_var).grid(row=1, column=5, sticky="w", pady=(6, 0))
        ttk.Checkbutton(export, text="Open SR in PulseView", variable=self.export_open_pv_var).grid(row=1, column=6, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))

        # ------------------------------------------------------------
        # 3) Capture settings: mode/rate/analog parameters.
        # ------------------------------------------------------------
        row += 1
        settings = ttk.LabelFrame(main, text="BitScope Capture", padding=8)
        settings.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        settings.columnconfigure(7, weight=1)

        self.capture_status_label = ttk.Label(settings, textvariable=self.capture_status_var, font=("Segoe UI", 9, "bold"))
        self.capture_status_label.grid(row=0, column=0, sticky="w", padx=(0, 10))

        ttk.Label(settings, text="Mode").grid(row=0, column=1, sticky="w")
        self.mode_box = ttk.Combobox(
            settings,
            textvariable=self.mode_var,
            values=list(MODE_CONFIGS.keys()),
            state="readonly",
            width=12,
        )
        self.mode_box.grid(row=0, column=2, sticky="w", padx=(6, 14))
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self._on_mode_changed(auto_apply=True))

        ttk.Label(settings, text="Requested rate").grid(row=0, column=3, sticky="w")
        self.rate_box = ttk.Combobox(
            settings,
            textvariable=self.rate_var,
            values=rate_options_for_mode(self.mode_var.get()),
            state="readonly",
            width=16,
        )
        self.rate_box.grid(row=0, column=4, sticky="w", padx=(6, 4))
        self.rate_box.bind("<<ComboboxSelected>>", self._auto_apply_settings)
        self.rate_box.bind("<Return>", self._auto_apply_settings)
        self.rate_box.bind("<FocusOut>", self._auto_apply_settings)
        ttk.Label(settings, text="Hz").grid(row=0, column=5, sticky="w", padx=(0, 14))

        ttk.Label(settings, text="MIXED ana_dig threshold").grid(row=0, column=6, sticky="w")
        threshold_entry = ttk.Entry(settings, textvariable=self.threshold_var, width=10)
        threshold_entry.grid(row=0, column=7, sticky="w", padx=(6, 14))
        self._bind_entry_auto_apply(threshold_entry)

        # Per-channel analog settings. CH1 -> ana0, CH2 -> ana1.
        row2 = 1
        ttk.Label(settings, text="Channel").grid(row=row2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="Physical input").grid(row=row2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="Range").grid(row=row2, column=2, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="Offset V").grid(row=row2, column=3, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="Probe").grid(row=row2, column=4, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="Factor").grid(row=row2, column=5, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="YINC").grid(row=row2, column=6, sticky="w", pady=(6, 0))
        ttk.Label(settings, text="YINC value").grid(row=row2, column=7, sticky="w", pady=(6, 0))

        def add_channel_row(r, label, source_var, range_var, offset_var, probe_mode_var, probe_factor_var, yinc_mode_var, yinc_var, channel):
            ttk.Label(settings, text=label).grid(row=r, column=0, sticky="w", pady=(4, 0))
            source_box = ttk.Combobox(settings, textvariable=source_var, values=GUI_ANALOG_SOURCE_OPTIONS, state="readonly", width=8)
            source_box.grid(row=r, column=1, sticky="w", padx=(6, 8), pady=(4, 0))
            source_box.bind("<<ComboboxSelected>>", self._auto_apply_settings)
            range_box = ttk.Combobox(settings, textvariable=range_var, values=list(BL_RANGE_OPTIONS.keys()), state="readonly", width=8)
            range_box.grid(row=r, column=2, sticky="w", padx=(0, 8), pady=(4, 0))
            range_box.bind("<<ComboboxSelected>>", lambda e, ch=channel: self._sync_probe_and_yinc_vars(ch))
            offset_entry = ttk.Entry(settings, textvariable=offset_var, width=8)
            offset_entry.grid(row=r, column=3, sticky="w", padx=(0, 8), pady=(4, 0))
            self._bind_entry_auto_apply(offset_entry)
            probe_box = ttk.Combobox(settings, textvariable=probe_mode_var, values=GUI_PROBE_OPTIONS, state="readonly", width=8)
            probe_box.grid(row=r, column=4, sticky="w", padx=(0, 8), pady=(4, 0))
            probe_box.bind("<<ComboboxSelected>>", lambda e, ch=channel: self._on_probe_mode_changed(ch))
            probe_entry = ttk.Entry(settings, textvariable=probe_factor_var, width=8)
            probe_entry.grid(row=r, column=5, sticky="w", padx=(0, 8), pady=(4, 0))
            self._bind_entry_auto_apply(probe_entry)
            yinc_box = ttk.Combobox(settings, textvariable=yinc_mode_var, values=GUI_YINC_MODE_OPTIONS, state="readonly", width=8)
            yinc_box.grid(row=r, column=6, sticky="w", padx=(0, 8), pady=(4, 0))
            yinc_box.bind("<<ComboboxSelected>>", lambda e, ch=channel: self._on_yinc_mode_changed(ch))
            yinc_entry = ttk.Entry(settings, textvariable=yinc_var, width=12)
            yinc_entry.grid(row=r, column=7, sticky="w", padx=(0, 8), pady=(4, 0))
            self._bind_entry_auto_apply(yinc_entry)

        add_channel_row(2, "CH1 / ana0", self.ch1_source_var, self.ch1_range_var, self.ch1_offset_var, self.ch1_probe_mode_var, self.ch1_probe_factor_var, self.ch1_yinc_mode_var, self.ch1_yinc_var, 1)
        add_channel_row(3, "CH2 / ana1", self.ch2_source_var, self.ch2_range_var, self.ch2_offset_var, self.ch2_probe_mode_var, self.ch2_probe_factor_var, self.ch2_yinc_mode_var, self.ch2_yinc_var, 2)

        # Simple hardware trigger controls. BitLib supports one trigger channel.
        trig_row = 4
        ttk.Label(settings, text="Trigger").grid(row=trig_row, column=0, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Mode").grid(row=trig_row, column=1, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Source").grid(row=trig_row, column=2, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Edge").grid(row=trig_row, column=3, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Level V").grid(row=trig_row, column=4, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Intro s").grid(row=trig_row, column=5, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Delay s").grid(row=trig_row, column=6, sticky="w", pady=(10, 0))
        ttk.Label(settings, text="Timeout s").grid(row=trig_row, column=7, sticky="w", pady=(10, 0))

        trig_row += 1
        self.trigger_status_label = ttk.Label(settings, textvariable=self.trigger_status_var, font=("Segoe UI", 9, "bold"))
        self.trigger_status_label.grid(row=trig_row, column=0, sticky="w", pady=(4, 0))
        self.trigger_mode_box = ttk.Combobox(settings, textvariable=self.trigger_mode_var, values=GUI_TRIGGER_MODE_OPTIONS, state="readonly", width=9)
        self.trigger_mode_box.grid(row=trig_row, column=1, sticky="w", padx=(6, 8), pady=(4, 0))
        self.trigger_mode_box.bind("<<ComboboxSelected>>", self._auto_apply_settings)

        self.trigger_source_box = ttk.Combobox(settings, textvariable=self.trigger_source_var, values=trigger_sources_for_mode(self.mode_var.get()), state="readonly", width=9)
        self.trigger_source_box.grid(row=trig_row, column=2, sticky="w", padx=(0, 8), pady=(4, 0))
        self.trigger_source_box.bind("<<ComboboxSelected>>", self._auto_apply_settings)

        self.trigger_edge_box = ttk.Combobox(settings, textvariable=self.trigger_edge_var, values=GUI_TRIGGER_EDGE_OPTIONS, state="readonly", width=8)
        self.trigger_edge_box.grid(row=trig_row, column=3, sticky="w", padx=(0, 8), pady=(4, 0))
        self.trigger_edge_box.bind("<<ComboboxSelected>>", self._auto_apply_settings)

        trigger_level_entry = ttk.Entry(settings, textvariable=self.trigger_level_var, width=8)
        trigger_level_entry.grid(row=trig_row, column=4, sticky="w", padx=(0, 8), pady=(4, 0))
        self._bind_entry_auto_apply(trigger_level_entry)
        trigger_intro_entry = ttk.Entry(settings, textvariable=self.trigger_intro_var, width=8)
        trigger_intro_entry.grid(row=trig_row, column=5, sticky="w", padx=(0, 8), pady=(4, 0))
        self._bind_entry_auto_apply(trigger_intro_entry)
        trigger_delay_entry = ttk.Entry(settings, textvariable=self.trigger_delay_var, width=8)
        trigger_delay_entry.grid(row=trig_row, column=6, sticky="w", padx=(0, 8), pady=(4, 0))
        self._bind_entry_auto_apply(trigger_delay_entry)
        trigger_timeout_entry = ttk.Entry(settings, textvariable=self.trigger_timeout_var, width=10)
        trigger_timeout_entry.grid(row=trig_row, column=7, sticky="w", padx=(0, 8), pady=(4, 0))
        self._bind_entry_auto_apply(trigger_timeout_entry)

        ttk.Label(settings, textvariable=self.last_capture_var).grid(row=trig_row + 1, column=0, columnspan=8, sticky="w", pady=(4, 0))

        # ------------------------------------------------------------
        # 4) Resizable help + logs. PanedWindow avoids the help panel eating
        #    vertical resize space; the user can drag the sash.
        # ------------------------------------------------------------
        row += 1
        body = ttk.PanedWindow(main, orient="vertical")
        body.grid(row=row, column=0, sticky="nsew")
        main.rowconfigure(row, weight=1)

        help_frame = ttk.LabelFrame(body, text="Help / current plan", padding=6)
        help_frame.columnconfigure(0, weight=1)
        help_frame.rowconfigure(0, weight=1)

        self.plan_text = tk.Text(help_frame, height=13, width=130, wrap="none")
        self.plan_text.grid(row=0, column=0, sticky="nsew")
        plan_scroll_y = ttk.Scrollbar(help_frame, orient="vertical", command=self.plan_text.yview)
        plan_scroll_y.grid(row=0, column=1, sticky="ns")
        plan_scroll_x = ttk.Scrollbar(help_frame, orient="horizontal", command=self.plan_text.xview)
        plan_scroll_x.grid(row=1, column=0, sticky="ew")
        self.plan_text.configure(yscrollcommand=plan_scroll_y.set, xscrollcommand=plan_scroll_x.set)

        logs_pane = ttk.PanedWindow(body, orient="horizontal")

        main_log_frame = ttk.LabelFrame(logs_pane, text="Main log / BitScope", padding=6)
        main_log_frame.columnconfigure(0, weight=1)
        main_log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(main_log_frame, height=18, width=78, wrap="none")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll_y = ttk.Scrollbar(main_log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll_y.grid(row=0, column=1, sticky="ns")
        log_scroll_x = ttk.Scrollbar(main_log_frame, orient="horizontal", command=self.log_text.xview)
        log_scroll_x.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=log_scroll_y.set, xscrollcommand=log_scroll_x.set)

        scpi_log_frame = ttk.LabelFrame(logs_pane, text="SCPI / PulseView debug", padding=6)
        scpi_log_frame.columnconfigure(0, weight=1)
        scpi_log_frame.rowconfigure(0, weight=1)

        self.scpi_log_text = tk.Text(scpi_log_frame, height=18, width=78, wrap="none")
        self.scpi_log_text.grid(row=0, column=0, sticky="nsew")
        scpi_scroll_y = ttk.Scrollbar(scpi_log_frame, orient="vertical", command=self.scpi_log_text.yview)
        scpi_scroll_y.grid(row=0, column=1, sticky="ns")
        scpi_scroll_x = ttk.Scrollbar(scpi_log_frame, orient="horizontal", command=self.scpi_log_text.xview)
        scpi_scroll_x.grid(row=1, column=0, sticky="ew")
        self.scpi_log_text.configure(yscrollcommand=scpi_scroll_y.set, xscrollcommand=scpi_scroll_x.set)

        logs_pane.add(main_log_frame, weight=3)
        logs_pane.add(scpi_log_frame, weight=2)

        body.add(help_frame, weight=1)
        body.add(logs_pane, weight=3)

    def _bind_entry_auto_apply(self, entry):
        entry.bind("<Return>", self._auto_apply_settings)
        entry.bind("<FocusOut>", self._auto_apply_settings)

    def _update_trigger_source_options(self):
        sources = trigger_sources_for_mode(self.mode_var.get())
        if hasattr(self, "trigger_source_box"):
            self.trigger_source_box.configure(values=sources)
        if sources and self.trigger_source_var.get() not in sources:
            self.trigger_source_var.set(sources[0])

    def _build_trigger_config_from_vars(self) -> dict | None:
        mode = self.trigger_mode_var.get()
        if mode not in GUI_TRIGGER_MODE_OPTIONS:
            raise ValueError("Invalid trigger mode.")

        if mode == "Forced":
            return {"enabled": False, "mode": "forced"}

        source = self.trigger_source_var.get()
        sources = trigger_sources_for_mode(self.mode_var.get())
        if source not in sources:
            raise ValueError(
                f"Trigger channel not available in mode {self.mode_var.get()}: {source}. "
                f"Available: {', '.join(sources)}"
            )

        edge = self.trigger_edge_var.get().upper()
        if edge not in GUI_TRIGGER_EDGE_OPTIONS:
            raise ValueError("Invalid trigger edge.")

        level = float(self.trigger_level_var.get())
        intro = float(self.trigger_intro_var.get())
        delay = float(self.trigger_delay_var.get())
        timeout = float(self.trigger_timeout_var.get())

        if intro < 0 or delay < 0 or timeout < 0:
            raise ValueError("Intro, delay and timeout must be >= 0.")
        if mode in ("Normal", "Auto") and timeout <= 0:
            raise ValueError("Trigger Normal/Auto requires timeout > 0.")

        return {
            "enabled": True,
            "mode": mode.lower(),
            "source": source,
            "edge": edge,
            "level": level,
            "intro_s": intro,
            "delay_s": delay,
            "timeout_s": timeout,
        }

    def _sync_probe_and_yinc_vars(self, channel: int):
        if channel == 1:
            probe_mode_var = self.ch1_probe_mode_var
            probe_factor_var = self.ch1_probe_factor_var
            range_var = self.ch1_range_var
            yinc_mode_var = self.ch1_yinc_mode_var
            yinc_var = self.ch1_yinc_var
        else:
            probe_mode_var = self.ch2_probe_mode_var
            probe_factor_var = self.ch2_probe_factor_var
            range_var = self.ch2_range_var
            yinc_mode_var = self.ch2_yinc_mode_var
            yinc_var = self.ch2_yinc_var

        mode = probe_mode_var.get()
        if mode == "1x":
            probe_factor_var.set("1.0")
        elif mode == "10x":
            probe_factor_var.set("10.0")

        if yinc_mode_var.get() == "auto":
            try:
                yinc_var.set(f"{auto_yinc_for(range_var.get(), float(probe_factor_var.get())):.9g}")
            except Exception:
                pass

        self._update_plan_text()
        self._auto_apply_settings()

    def _on_probe_mode_changed(self, channel: int):
        self._sync_probe_and_yinc_vars(channel)

    def _on_yinc_mode_changed(self, channel: int):
        self._sync_probe_and_yinc_vars(channel)

    def _update_status_labels(self):
        if self.simulator_var.get():
            self.status_var.set("Simulator")
            try:
                self.status_label.configure(foreground="orange")
            except Exception:
                pass
        else:
            self.status_var.set("BitScope")
            try:
                self.status_label.configure(foreground="green")
            except Exception:
                pass

        try:
            running = bool(self.server and self.server.thread and self.server.thread.is_alive())
        except Exception:
            running = False

        if running:
            self.server_status_var.set(f"Proxy running on {HOST}:{PORT} | Clients: {self.client_count}")
            try:
                if self.client_count == 0:
                    self.server_status_label.configure(foreground="orange")
                elif self.client_count == 1:
                    self.server_status_label.configure(foreground="green")
                else:
                    self.server_status_label.configure(foreground="red")
            except Exception:
                pass
        else:
            self.server_status_var.set(f"Proxy stopped on {HOST}:{PORT} | Clients: 0")
            try:
                self.server_status_label.configure(foreground="gray")
            except Exception:
                pass

        bitness = python_bits()
        self.python_status_var.set(f"Python {bitness}-bit" + (" OK" if bitness == 32 else " WARNING: BitLib needs 32-bit"))

    def _copy_pulseview_command(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(DEFAULT_PULSEVIEW_CMD)
        self._log("Copied PulseView command to clipboard:")
        self._log(DEFAULT_PULSEVIEW_CMD)

    def _log(self, msg):
        self.log_queue.put(str(msg))

    def _scpi_log(self, msg):
        self.scpi_log_queue.put(str(msg))

    def _status_event(self, kind, state, detail=""):
        self.status_queue.put((kind, state, detail))

    def _set_capture_indicator(self, state: str, detail: str = ""):
        state = str(state or "").lower()
        if state == "capturing":
            text, color = "● Capturing", "orange"
        elif state == "ready":
            text, color = "● Ready", "green"
        elif state == "error":
            text, color = "● Error", "red"
        elif state in ("stopped", "idle"):
            text, color = "● Idle", "gray"
        else:
            text, color = f"● {state or 'Unknown'}", "gray"
        self.capture_status_var.set(text)
        if detail:
            self.last_capture_var.set(f"Last: {detail}")
        try:
            self.capture_status_label.configure(foreground=color)
        except Exception:
            pass

    def _set_trigger_indicator(self, state: str, detail: str = ""):
        state = str(state or "").lower()
        label_map = {
            "forced": ("● Forced", "gray"),
            "waiting": ("● Armed", "orange"),
            "simulator": ("● Sim", "orange"),
            "likely_triggered": ("● Triggered", "green"),
            "likely_triggered_no_event_found": ("● Triggered?", "orange"),
            "timeout_auto_with_event_in_buffer": ("● Auto+event", "orange"),
            "timeout_auto_no_event_found": ("● Timeout", "red"),
            "trace_failed": ("● Failed", "red"),
            "unknown": ("● Unknown", "purple"),
        }
        text, color = label_map.get(state, (f"● {state or 'Unknown'}", "gray"))
        if detail and state not in ("forced", "waiting"):
            text = f"{text}"
        self.trigger_status_var.set(text)
        try:
            self.trigger_status_label.configure(foreground=color)
        except Exception:
            pass

    def _handle_status_event(self, kind, state, detail=""):
        if kind == "capture":
            self._set_capture_indicator(state, detail)
        elif kind == "trigger":
            self._set_trigger_indicator(state, detail)
        elif kind == "clients":
            try:
                self.client_count = int(state)
            except Exception:
                self.client_count = 0
            self._update_status_labels()

    def _poll_log_queue(self):
        try:
            while True:
                kind, state, detail = self.status_queue.get_nowait()
                self._handle_status_event(kind, state, detail)
        except queue.Empty:
            pass

        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass

        try:
            while True:
                msg = self.scpi_log_queue.get_nowait()
                self.scpi_log_text.insert("end", msg + "\n")
                self.scpi_log_text.see("end")
        except queue.Empty:
            pass

        self.root.after(100, self._poll_log_queue)

    def _clear_log(self):
        self.log_text.delete("1.0", "end")
        self.scpi_log_text.delete("1.0", "end")

    def _auto_apply_settings(self, event=None):
        # Apply synchronously. Using after_idle left a small race: if PulseView
        # was started immediately after changing the combobox, the first capture
        # could still use the previous rate/XINC and only the second one was right.
        self._apply_settings()

    def _on_mode_changed(self, auto_apply: bool = False):
        mode = self.mode_var.get()
        rates = rate_options_for_mode(mode)
        self.rate_box.configure(values=rates)
        self._update_trigger_source_options()

        current_rate = self.rate_var.get().strip()
        if current_rate not in rates:
            default = MODE_CONFIGS[mode]["default_rate"]
            self.rate_var.set(default if default in rates else rates[0])

        self._update_plan_text()
        if auto_apply:
            self._apply_settings()

    def _selected_rate(self) -> float:
        return probed_actual_rate(self.mode_var.get(), self.rate_var.get())

    def _update_plan_text(self):
        mode = self.mode_var.get()
        requested_rate_text = self.rate_var.get().strip()
        limit = probed_limit_for(mode, requested_rate_text)
        rate = float(limit["actual_rate_hz"])
        rec = recommended_plan_for_mode(mode)
        live = live_plan_for_mode(mode)
        mem = memory_plan_for_mode(mode)

        def line_for(plan: CapturePlan) -> str:
            good_time = plan.real_samples / rate if rate > 0 else 0.0
            analog_pad = max(0, plan.analog_output_samples - plan.real_samples) if plan.analog_output_samples else 0
            logic_pad = max(0, plan.logic_output_samples - plan.real_samples) if plan.logic_output_samples else 0
            return (
                f"{plan.pv_recommended_source:<10} | "
                f"real={plan.real_samples:5d} | "
                f"T_good={fmt_time(good_time):>12s} | "
                f"outA={plan.analog_output_samples:5d} padA={analog_pad:5d} | "
                f"outLA={plan.logic_output_samples:5d} padLA={logic_pad:5d} | "
                f"segments={plan.segment_count}"
            )

        if mode == "FAST":
            channels = "CH1 ON, CH2 OFF, LA OFF"
        elif mode == "DUAL":
            channels = "CH1 ON, CH2 ON, LA OFF"
        elif mode == "LOGIC":
            channels = "CH1 OFF, CH2 OFF, LA ON; D0..D5 as needed"
        else:
            channels = "CH1/CH2 ON, LA ON; D0..D5 + D6/D7 derived"

        text = []
        text.append(f"Data source: {'Simulator - no real BitScope data' if self.simulator_var.get() else 'BitScope - real capture'}")
        text.append(f"Python: {python_bits()}-bit" + (" OK" if is_32bit_python() else "  (WARNING: 32-bit BitLib.dll requires 32-bit Python)"))
        text.append("")
        text.append(f"Selected mode: {mode}")
        text.append(f"Requested BitLib rate: {requested_rate_text} Hz")
        text.append(f"Measured/expected actual rate: {fmt_rate(rate)} ({rate:g} Hz)")
        text.append("Analog CH1: " +
                    f"physical input {self.ch1_source_var.get()} / {self.ch1_range_var.get()} / offset {self.ch1_offset_var.get()} V / " +
                    f"probe {self.ch1_probe_factor_var.get()}x ({self.ch1_probe_mode_var.get()}) / " +
                    f"YINC {self.ch1_yinc_var.get()} ({self.ch1_yinc_mode_var.get()})")
        text.append("Analog CH2: " +
                    f"physical input {self.ch2_source_var.get()} / {self.ch2_range_var.get()} / offset {self.ch2_offset_var.get()} V / " +
                    f"probe {self.ch2_probe_factor_var.get()}x ({self.ch2_probe_mode_var.get()}) / " +
                    f"YINC {self.ch2_yinc_var.get()} ({self.ch2_yinc_mode_var.get()})")
        text.append("Nota BitScope Micro: la physical input POD/BNC es informativa; BitLib se fuerza internamente a source=POD.")
        text.append("Auto YINC: approx range*probe/127. Conservative to avoid clipping in PulseView; use manual for more resolution or wider displayed range.")
        text.append(
            "Trigger: "
            f"{self.trigger_mode_var.get()} / source {self.trigger_source_var.get()} / "
            f"edge {self.trigger_edge_var.get()} / level {self.trigger_level_var.get()} V / "
            f"intro {self.trigger_intro_var.get()} s / delay {self.trigger_delay_var.get()} s / "
            f"timeout {self.trigger_timeout_var.get()} s"
        )
        text.append("Trigger Forced: free-run capture. Normal/Auto: simple hardware trigger on one channel; if it does not trigger, BitLib may still return an auto capture after timeout.")
        if float(requested_rate_text) != rate:
            text.append("WARNING: requested rate differs from actual rate; the proxy uses the actual rate for timing.")
        text.append("")
        text.append("Valid/useful options at this actual rate:")
        text.append(line_for(live))
        text.append(line_for(mem))
        if rec.pv_recommended_source != mem.pv_recommended_source:
            text.append(line_for(rec))
        text.append("")
        text.append("Recommended for this mode:")
        text.append(f"  PulseView Data source: {rec.pv_recommended_source}")
        text.append(f"  PulseView channels: {channels}")
        text.append(f"  Proxy rate real: {fmt_rate(rate)}")
        text.append(f"  Good duration: {fmt_time(rec.real_samples / rate if rate > 0 else 0)}")
        text.append(f"  Note: {rec.notes}")
        text.append("")
        text.append("Measured limits for this mode:")
        text.append("  requested      actual         max_samples   max_time")
        text.append("  -----------------------------------------------------")
        for _, requested, actual, max_samples, max_time in limit_rows_for_mode(mode):
            mark = "<--" if requested == requested_rate_text else ""
            text.append(
                f"  {requested:>9s} Hz  {fmt_rate(actual):>12s}  "
                f"{max_samples:>7d}      {fmt_time(max_time):>12s} {mark}"
            )
        text.append("")
        text.append("Start PulseView:")
        text.append(f"  {DEFAULT_PULSEVIEW_CMD}")
        text.append("")
        text.append("Rule: timing is correct up to real_samples. Hold-last padding only fills the tail.")

        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", "\n".join(text))

    def _apply_settings(self):
        try:
            self.settings.mode = self.mode_var.get()
            self.settings.rate_text = self.rate_var.get()
            self.settings.analog_threshold = float(self.threshold_var.get())

            def read_channel(channel: int):
                if channel == 1:
                    source_var = self.ch1_source_var
                    range_var = self.ch1_range_var
                    offset_var = self.ch1_offset_var
                    probe_mode_var = self.ch1_probe_mode_var
                    probe_factor_var = self.ch1_probe_factor_var
                    yinc_mode_var = self.ch1_yinc_mode_var
                    yinc_var = self.ch1_yinc_var
                else:
                    source_var = self.ch2_source_var
                    range_var = self.ch2_range_var
                    offset_var = self.ch2_offset_var
                    probe_mode_var = self.ch2_probe_mode_var
                    probe_factor_var = self.ch2_probe_factor_var
                    yinc_mode_var = self.ch2_yinc_mode_var
                    yinc_var = self.ch2_yinc_var

                probe_factor = probe_factor_from_mode(probe_mode_var.get(), probe_factor_var.get())
                probe_factor_var.set(f"{probe_factor:g}")

                if yinc_mode_var.get() == "auto":
                    yinc = auto_yinc_for(range_var.get(), probe_factor)
                    yinc_var.set(f"{yinc:.9g}")
                else:
                    yinc = float(yinc_var.get())
                    if yinc <= 0:
                        raise ValueError("YINC must be positive.")

                return {
                    "source_name": source_var.get(),
                    "range_label": range_var.get(),
                    "offset": float(offset_var.get()),
                    "probe_mode": probe_mode_var.get(),
                    "probe_factor": probe_factor,
                    "yinc_mode": yinc_mode_var.get(),
                    "yinc": yinc,
                }

            ch1 = read_channel(1)
            ch2 = read_channel(2)

            self.settings.ch1_source_name = ch1["source_name"]
            self.settings.ch1_range_label = ch1["range_label"]
            self.settings.ch1_offset = ch1["offset"]
            self.settings.ch1_probe_mode = ch1["probe_mode"]
            self.settings.ch1_probe_factor = ch1["probe_factor"]
            self.settings.ch1_yinc_mode = ch1["yinc_mode"]
            self.settings.ch1_yinc = ch1["yinc"]

            self.settings.ch2_source_name = ch2["source_name"]
            self.settings.ch2_range_label = ch2["range_label"]
            self.settings.ch2_offset = ch2["offset"]
            self.settings.ch2_probe_mode = ch2["probe_mode"]
            self.settings.ch2_probe_factor = ch2["probe_factor"]
            self.settings.ch2_yinc_mode = ch2["yinc_mode"]
            self.settings.ch2_yinc = ch2["yinc"]

            trigger_cfg = self._build_trigger_config_from_vars()
            self.settings.trigger_mode = self.trigger_mode_var.get()
            self.settings.trigger_source = self.trigger_source_var.get()
            self.settings.trigger_edge = self.trigger_edge_var.get().upper()
            self.settings.trigger_level = float(self.trigger_level_var.get())
            self.settings.trigger_intro_s = float(self.trigger_intro_var.get())
            self.settings.trigger_delay_s = float(self.trigger_delay_var.get())
            self.settings.trigger_timeout_s = float(self.trigger_timeout_var.get())

            self.settings.use_simulator_data = bool(self.simulator_var.get())
        except Exception as e:
            messagebox.showerror("Settings", str(e))
            return
        self._update_status_labels()
        self._update_plan_text()
        if self.settings.trigger_mode == "Forced":
            self._set_trigger_indicator("forced", "")
        else:
            self._set_trigger_indicator("waiting", "")
        self._log(
            f"Settings applied: mode={self.settings.mode}, rate={self.settings.rate_text}, "
            f"CH1 physical={self.settings.ch1_source_name}/{self.settings.ch1_range_label}/probe{self.settings.ch1_probe_factor}x/YINC={self.settings.ch1_yinc}, "
            f"CH2 physical={self.settings.ch2_source_name}/{self.settings.ch2_range_label}/probe{self.settings.ch2_probe_factor}x/YINC={self.settings.ch2_yinc}, "
            f"trigger={self.settings.trigger_mode}/{self.settings.trigger_source}/{self.settings.trigger_edge}/"
            f"level={self.settings.trigger_level}/intro={self.settings.trigger_intro_s}/"
            f"delay={self.settings.trigger_delay_s}/timeout={self.settings.trigger_timeout_s}, "
            f"BitLib source=POD forced, simulator={self.settings.use_simulator_data}"
        )
        if self.settings.use_simulator_data:
            self._log("WARNING: Simulator enabled. PulseView will not show real BitScope data.")
        if self.server:
            self.server.read_offsets = {"CHAN1": 0, "CHAN2": 0, "LA": 0}
            self.server.capture.mark_dirty("settings changed")

        self._save_gui_settings()

    def _start_server(self):
        self._apply_settings()
        if not self.settings.use_simulator_data and not is_32bit_python():
            msg = "BitLib.dll is 32-bit. Run this proxy with Python 3.11 32-bit, or enable Simulator mode."
            self._log("ERROR: " + msg)
            messagebox.showerror("Python architecture", msg)
            return
        try:
            self.client_count = 0
            self.server = RigolScpiServer(self.settings, self._log, self._scpi_log, self._status_event)
            self.server.start()
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self._update_status_labels()
        except Exception as e:
            messagebox.showerror("Start proxy", str(e))

    def _stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None
        self.client_count = 0
        self._set_capture_indicator("idle", "")
        self._set_trigger_indicator("forced", "")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._update_status_labels()

    def _mark_recapture(self):
        if self.server:
            self.server.capture.mark_dirty("manual recapture")
        else:
            self._log("Proxy is not running")

    def _choose_export_prefix(self):
        current = Path(self.export_prefix_var.get())
        selected = filedialog.asksaveasfilename(
            title="Capture base name",
            initialdir=str(current.parent if current.parent.exists() else Path.cwd()),
            initialfile=current.name,
            defaultextension="",
            filetypes=[("Nombre base", "*.*")],
        )
        if selected:
            self.export_prefix_var.set(str(Path(selected).with_suffix("")))

    def _export_prefix_with_timestamp(self) -> str:
        prefix = self.export_prefix_var.get().strip()
        if not prefix:
            raise ValueError("Output base name is empty.")
        if self.export_timestamp_var.get():
            p = Path(prefix)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = str(p.parent / f"{p.name}_{stamp}")
        return prefix

    def _start_export_capture(self):
        if self.export_worker and self.export_worker.is_alive():
            messagebox.showwarning("Export in progress", "A capture/export is already in progress.")
            return

        if not any([
            self.export_csv_var.get(),
            self.export_sigrok_csv_var.get(),
            self.export_svg_var.get(),
            self.export_vcd_var.get(),
            self.export_sr_var.get(),
        ]):
            messagebox.showerror("No output selected", "Select at least one output format.")
            return

        try:
            self._apply_settings()
            prefix = self._export_prefix_with_timestamp()
            mode = self.settings.mode
            requested_rate = float(self.settings.rate_text)
            threshold = float(self.threshold_var.get())
        except Exception as e:
            messagebox.showerror("Export", str(e))
            return

        if not self.settings.use_simulator_data and not is_32bit_python():
            msg = "BitLib.dll is 32-bit. Run with Python 3.11 32-bit."
            messagebox.showerror("Python architecture", msg)
            return

        self.export_button.configure(state="disabled")
        self.export_worker = threading.Thread(
            target=self._export_worker,
            args=(mode, requested_rate, prefix, threshold),
            daemon=True,
        )
        self.export_worker.start()

    def _export_worker(self, mode, requested_rate, prefix, threshold):
        try:
            self._log("")
            self._log("=" * 72)
            self._log("Export capture to files")
            self._log(f"mode={mode}, requested_rate={requested_rate:g}, prefix={prefix}")
            self._log(
                f"CH1 physical={self.settings.ch1_source_name}, BitLib source=POD, range={self.settings.ch1_range_label}, "
                f"offset={self.settings.ch1_offset}, probe={self.settings.ch1_probe_factor}x, YINC={self.settings.ch1_yinc}"
            )
            self._log(
                f"CH2 physical={self.settings.ch2_source_name}, BitLib source=POD, range={self.settings.ch2_range_label}, "
                f"offset={self.settings.ch2_offset}, probe={self.settings.ch2_probe_factor}x, YINC={self.settings.ch2_yinc}"
            )
            self._log("duration=max")
            self._log(
                f"trigger={self.settings.trigger_mode}, source={self.settings.trigger_source}, "
                f"edge={self.settings.trigger_edge}, level={self.settings.trigger_level}, "
                f"intro={self.settings.trigger_intro_s}, delay={self.settings.trigger_delay_s}, "
                f"timeout={self.settings.trigger_timeout_s}"
            )
            self._log("=" * 72)

            result = capture_to_files(
                mode_name=mode,
                requested_rate=requested_rate,
                requested_time=0.0,
                output_prefix=prefix,
                make_csv=self.export_csv_var.get(),
                make_sigrok_csv=self.export_sigrok_csv_var.get(),
                make_svg=self.export_svg_var.get(),
                make_vcd=self.export_vcd_var.get(),
                make_sr=self.export_sr_var.get(),
                analog_digital_threshold=threshold,
                trigger_config=self.settings.trigger_config(),
                analog_channel_settings=self.settings.analog_channel_settings(),
                log=self._log,
            )

            self._log("")
            self._log("EXPORT OK")
            self._log(f"actual_rate={result['actual_rate']} Hz")
            self._log(f"samples={result['actual_size']}")
            self._log(f"time={result['actual_time']} s")
            if result.get("trigger"):
                tr = result["trigger"]
                self._log(
                    f"trigger_status={tr.get('trigger_status')}, "
                    f"event_index={tr.get('event_index')}, elapsed={tr.get('elapsed_s')}"
                )

            if self.export_open_pv_var.get() and self.export_sr_var.get():
                sr_file = str(Path(prefix).with_suffix(".sr"))
                self._open_pulseview_file(sr_file)

        except Exception as e:
            msg = str(e)
            self._log("")
            self._log("EXPORT ERROR:")
            self._log(msg)
            self._log(traceback.format_exc())
            self.root.after(0, lambda: messagebox.showerror("Export", msg))
        finally:
            self.root.after(0, lambda: self.export_button.configure(state="normal"))

    def _open_pulseview_file(self, sr_file: str):
        try:
            subprocess.Popen([
                DEFAULT_PULSEVIEW_PATH,
                "--dont-scan",
                sr_file,
            ])
            self._log(f"Opening PulseView file: {sr_file}")
        except Exception as e:
            self._log(f"ERROR opening PulseView file: {e}")

    def _open_pulseview(self):
        cmd = [
            DEFAULT_PULSEVIEW_PATH,
            "--dont-scan",
            "-d",
            f"rigol-ds:conn=tcp-raw/{HOST}/{PORT}",
            "-l",
            "5",
        ]
        try:
            subprocess.Popen(cmd)
            self._log("Opening PulseView: " + DEFAULT_PULSEVIEW_CMD)
        except Exception as e:
            messagebox.showerror("PulseView", f"Could not open PulseView:\n{e}")
            self._log(f"ERROR opening PulseView: {e}")


def main():
    root = tk.Tk()
    ProxyGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
