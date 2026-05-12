# Pruebas rápidas
# Lista modos y tiempos:
#   python .\bitscope_cli.py --list
# Captura normal MIXED, solo .sr, máximo tiempo:
#   python .\bitscope_cli.py --mode MIXED --rate 10000 --out captures\mixed_test
# Captura y abre PulseView sin escaneo:
#   python .\bitscope_cli.py --mode MIXED --rate 10000 --out captures\mixed_test --open-pulseview
# Generar todo para depuración:
#   python .\bitscope_cli.py --mode MIXED --rate 10000 --out captures\mixed_debug --all
# Solo lógica a 1 MHz:
#   python .\bitscope_cli.py --mode LOGIC --rate 1000000 --out captures\logic_1mhz
# FAST analógico por BNC, rango 9.2 V, sonda 10x:
#   python .\bitscope_cli.py --mode FAST --rate 100000 --ch1-source BNC --ch1-range "9.2 V" --ch1-probe 10x --out captures\fast_bnc_10x
# DUAL con CH1 BNC 1x y CH2 POD 10x:
#   python .\bitscope_cli.py --mode DUAL --rate 100000 --ch1-source BNC --ch1-probe 1x --ch2-source POD --ch2-probe 10x --out captures\dual_test
#!/usr/bin/env python3
"""BitScope Micro command-line capture tool.

This CLI matches the GUI/proxy model:
  - BitLib analog source is forced to POD for this BitScope Micro.
  - POD/BNC is kept only as a physical connection note.
  - Probe scaling is applied in software so exports show physical voltage.
  - Optional simple hardware trigger on one channel: ana0/ana1/dig0..dig5.

Examples:
  python bitscope_cli.py --list
  python bitscope_cli.py --mode LOGIC --rate 1000000 --out captures/uart \
      --trigger normal --trigger-source dig0 --trigger-edge FALL \
      --trigger-level 1.5 --trigger-intro 0.0005 --trigger-timeout 0.5
  python bitscope_cli.py --mode MIXED --rate 100000 --out captures/mixed \
      --trigger normal --trigger-source ana1 --trigger-edge RISE \
      --trigger-level 1.6 --trigger-intro 0.002
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from bitscope_core import (
    MODE_CONFIGS,
    RATE_OPTIONS,
    BL_RANGE_OPTIONS,
    BL_SOURCE_OPTIONS,
    describe_modes,
    get_max_capture_time_hint,
    capture_to_files,
)

DEFAULT_PULSEVIEW_PATH = r"C:\Program Files\sigrok\PulseView_N\pulseview.exe"

PHYSICAL_INPUT_OPTIONS = ["POD", "BNC"]
PROBE_OPTIONS = ["1x", "10x", "custom"]
TRIGGER_MODES = ["forced", "normal", "auto"]
TRIGGER_EDGES = ["RISE", "FALL", "HIGH", "LOW"]


def print_modes() -> None:
    current_mode = None

    for row in describe_modes():
        mode = row["mode"]

        if mode != current_mode:
            current_mode = mode
            print()
            print("=" * 72)
            print(mode)
            print("=" * 72)

        print(
            f"rate={row['rate_text']:>10s} Hz  "
            f"actual={row['actual_rate']:>12.3f} Hz  "
            f"samples={row['max_samples']:>6d}  "
            f"max_time={row['max_time']:.6g} s"
        )


def trigger_sources_for_mode(mode: str) -> list[str]:
    cfg = MODE_CONFIGS[mode]
    return list(cfg["analog"].keys()) + list(cfg["digital"].keys())


def default_trigger_source(mode: str) -> str:
    sources = trigger_sources_for_mode(mode)
    if not sources:
        raise ValueError(f"No trigger-capable channels in mode {mode}")
    # For pure logic, the first source is dig0. For analog/mixed it is ana0.
    return sources[0]


def parse_probe_factor(probe_mode: str, custom_text: str | float) -> float:
    if probe_mode == "1x":
        return 1.0
    if probe_mode == "10x":
        return 10.0
    factor = float(custom_text)
    if factor <= 0:
        raise ValueError("Probe factor must be positive.")
    return factor


def build_trigger_config(args, mode: str) -> dict:
    trigger_mode = str(args.trigger).lower()
    if trigger_mode == "forced":
        return {"enabled": False, "mode": "forced"}

    source = args.trigger_source or default_trigger_source(mode)
    valid_sources = trigger_sources_for_mode(mode)
    if source not in valid_sources:
        raise ValueError(
            f"Trigger source {source!r} is not available in mode {mode}. "
            f"Available: {', '.join(valid_sources)}"
        )

    if args.trigger_intro < 0 or args.trigger_delay < 0 or args.trigger_timeout < 0:
        raise ValueError("Trigger intro, delay and timeout must be >= 0.")
    if args.trigger_timeout <= 0:
        raise ValueError("Normal/Auto trigger requires --trigger-timeout > 0.")

    return {
        "enabled": True,
        "mode": trigger_mode,
        "source": source,
        "edge": args.trigger_edge.upper(),
        "level": float(args.trigger_level),
        "intro_s": float(args.trigger_intro),
        "delay_s": float(args.trigger_delay),
        "timeout_s": float(args.trigger_timeout),
    }


def add_channel_arguments(parser: argparse.ArgumentParser, prefix: str, label: str) -> None:
    group = parser.add_argument_group(f"{label} analog settings")
    group.add_argument(
        f"--{prefix}-input",
        choices=PHYSICAL_INPUT_OPTIONS,
        default="BNC",
        help="Physical connection note only. BitLib source is forced to POD on this BitScope Micro.",
    )
    group.add_argument(
        f"--{prefix}-range",
        choices=list(BL_RANGE_OPTIONS.keys()),
        default="9.2 V",
        help="BitScope analog range label. Quote it on Windows, e.g. --ch1-range \"9.2 V\".",
    )
    group.add_argument(
        f"--{prefix}-offset",
        type=float,
        default=0.0,
        help="Analog offset in volts.",
    )
    group.add_argument(
        f"--{prefix}-probe",
        choices=PROBE_OPTIONS,
        default="1x",
        help="Physical probe attenuation. Exported values are scaled by this factor.",
    )
    group.add_argument(
        f"--{prefix}-probe-factor",
        type=float,
        default=1.0,
        help="Custom probe factor, used only when --*-probe custom.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BitScope Micro capture tool: FAST, DUAL, MIXED, LOGIC -> CSV/SVG/VCD/SR, with optional simple trigger."
    )

    parser.add_argument("--list", action="store_true", help="List modes, rates and approximate maximum capture time.")
    parser.add_argument("--mode", choices=list(MODE_CONFIGS.keys()), default="MIXED", help="Capture mode.")
    parser.add_argument("--rate", default=None, help="Requested rate in Hz. Defaults to the selected mode default.")
    parser.add_argument("--time", "--duration", dest="duration", type=float, default=None, help="Requested duration in seconds. Default: maximum possible.")
    parser.add_argument("--max-time", action="store_true", help="Print maximum duration for mode/rate and exit.")
    parser.add_argument("--out", default=str(Path.cwd() / "capture"), help="Output base name without extension, e.g. captures/uart_test")
    parser.add_argument("--timestamp", action="store_true", help="Append date/time to the output base name.")

    output = parser.add_argument_group("output formats")
    output.add_argument("--csv", action="store_true", help="Generate full CSV.")
    output.add_argument("--sigrok-csv", action="store_true", help="Keep intermediate sigrok-compatible CSV.")
    output.add_argument("--svg", action="store_true", help="Generate quick-look SVG.")
    output.add_argument("--vcd", action="store_true", help="Generate VCD for digital signals.")
    output.add_argument("--no-sr", action="store_true", help="Do not generate .sr. By default .sr is generated.")
    output.add_argument("--all", action="store_true", help="Generate full CSV, sigrok CSV, SVG, VCD and SR.")
    output.add_argument("--open-pulseview", action="store_true", help="Open resulting .sr in PulseView with --dont-scan.")
    output.add_argument("--pulseview-path", default=DEFAULT_PULSEVIEW_PATH, help="Path to pulseview.exe.")

    analog = parser.add_argument_group("mixed analog-to-digital derived channels")
    analog.add_argument("--threshold", type=float, default=1.5, help="Threshold for derived ana0_dig/ana1_dig in MIXED mode.")

    add_channel_arguments(parser, "ch1", "CH1 / ana0")
    add_channel_arguments(parser, "ch2", "CH2 / ana1")

    trigger = parser.add_argument_group("simple hardware trigger")
    trigger.add_argument(
        "--trigger",
        choices=TRIGGER_MODES,
        default="forced",
        help="forced: free-run. normal/auto: simple BitLib trigger on one selected channel.",
    )
    trigger.add_argument(
        "--trigger-source",
        default=None,
        help="Trigger source: ana0, ana1, dig0..dig5 depending on mode. Default: first available source.",
    )
    trigger.add_argument("--trigger-edge", choices=TRIGGER_EDGES, default="FALL", help="Trigger edge/condition.")
    trigger.add_argument("--trigger-level", type=float, default=1.5, help="Trigger level in volts. Analog level is physical voltage; probe factor is compensated.")
    trigger.add_argument("--trigger-intro", type=float, default=0.0, help="Pre-trigger/intro time in seconds.")
    trigger.add_argument("--trigger-delay", type=float, default=0.0, help="Post-trigger delay in seconds. Keep 0.0 unless needed.")
    trigger.add_argument("--trigger-timeout", type=float, default=0.5, help="Trigger wait timeout in seconds for normal/auto.")
    trigger.add_argument("--list-trigger-sources", action="store_true", help="Print trigger sources for the selected mode and exit.")

    return parser.parse_args()


def build_analog_channel_settings(args) -> dict:
    ch1_probe = parse_probe_factor(args.ch1_probe, args.ch1_probe_factor)
    ch2_probe = parse_probe_factor(args.ch2_probe, args.ch2_probe_factor)

    return {
        "ana0": {
            "source": BL_SOURCE_OPTIONS["POD"],
            "range_index": BL_RANGE_OPTIONS[args.ch1_range],
            "offset": float(args.ch1_offset),
            "probe_factor": ch1_probe,
        },
        "ana1": {
            "source": BL_SOURCE_OPTIONS["POD"],
            "range_index": BL_RANGE_OPTIONS[args.ch2_range],
            "offset": float(args.ch2_offset),
            "probe_factor": ch2_probe,
        },
    }


def main() -> None:
    args = parse_args()

    if args.list:
        print_modes()
        return

    mode = args.mode

    if args.list_trigger_sources:
        print(f"{mode} trigger sources: {', '.join(trigger_sources_for_mode(mode))}")
        return

    rate_text = str(args.rate) if args.rate is not None else MODE_CONFIGS[mode]["default_rate"]

    if rate_text not in RATE_OPTIONS[mode]:
        print()
        print(f"WARNING: {rate_text} Hz is not in the recommended rate list for {mode}.")
        print("BitLib will still be asked for it and will return the actual accepted rate.")
        print()

    if args.max_time:
        max_time = get_max_capture_time_hint(mode, rate_text)
        print(f"{mode} @ {rate_text} Hz -> max_time ≈ {max_time:.6g} s")
        return

    duration = 0.0 if args.duration is None else float(args.duration)

    if args.all:
        make_csv = True
        make_sigrok_csv = True
        make_svg = True
        make_vcd = True
        make_sr = True
    else:
        make_csv = args.csv
        make_sigrok_csv = args.sigrok_csv
        make_svg = args.svg
        make_vcd = args.vcd
        make_sr = not args.no_sr

    if not any([make_csv, make_sigrok_csv, make_svg, make_vcd, make_sr]):
        raise SystemExit("No output format selected.")

    output_prefix = args.out
    if args.timestamp:
        p = Path(output_prefix)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_prefix = str(p.parent / f"{p.name}_{stamp}")

    analog_settings = build_analog_channel_settings(args)
    trigger_config = build_trigger_config(args, mode)

    print()
    print("=" * 72)
    print("BitScope CLI capture")
    print("=" * 72)
    print(f"mode       = {mode}")
    print(f"rate       = {rate_text} Hz")
    print(f"duration   = {'max' if duration <= 0 else duration}")
    print(f"out        = {output_prefix}")
    print(f"threshold  = {args.threshold} V")
    print(f"outputs    = csv={make_csv}, sigrok_csv={make_sigrok_csv}, svg={make_svg}, vcd={make_vcd}, sr={make_sr}")
    print()
    print("Analog inputs")
    print("-------------")
    print(f"CH1/ana0 physical={args.ch1_input}, BitLib source=POD, range={args.ch1_range}, offset={args.ch1_offset}, probe={analog_settings['ana0']['probe_factor']}x")
    print(f"CH2/ana1 physical={args.ch2_input}, BitLib source=POD, range={args.ch2_range}, offset={args.ch2_offset}, probe={analog_settings['ana1']['probe_factor']}x")
    print()
    print("Trigger")
    print("-------")
    if trigger_config.get("enabled"):
        print(
            f"mode={trigger_config['mode']}, source={trigger_config['source']}, "
            f"edge={trigger_config['edge']}, level={trigger_config['level']} V, "
            f"intro={trigger_config['intro_s']} s, delay={trigger_config['delay_s']} s, "
            f"timeout={trigger_config['timeout_s']} s"
        )
    else:
        print("mode=forced/free-run")
    print("=" * 72)
    print()

    result = capture_to_files(
        mode_name=mode,
        requested_rate=float(rate_text),
        requested_time=duration,
        output_prefix=output_prefix,
        make_csv=make_csv,
        make_sigrok_csv=make_sigrok_csv,
        make_svg=make_svg,
        make_vcd=make_vcd,
        make_sr=make_sr,
        analog_digital_threshold=args.threshold,
        trigger_config=trigger_config,
        analog_channel_settings=analog_settings,
        log=print,
    )

    print()
    print("CAPTURE OK")
    print(f"actual_rate = {result['actual_rate']} Hz")
    print(f"samples     = {result['actual_size']}")
    print(f"time        = {result['actual_time']} s")

    trig = result.get("trigger") or {}
    if trig:
        print()
        print("Trigger result")
        print("--------------")
        print(f"status      = {trig.get('trigger_status')}")
        print(f"source      = {trig.get('source')}")
        print(f"edge        = {trig.get('edge')}")
        print(f"event_index = {trig.get('event_index')}")
        print(f"event_time  = {trig.get('event_time_s')} s")
        print(f"elapsed     = {trig.get('elapsed_s')} s")

    if args.open_pulseview and make_sr:
        sr_file = str(Path(output_prefix).with_suffix(".sr"))
        print()
        print(f"Opening PulseView: {sr_file}")
        subprocess.Popen([args.pulseview_path, "--dont-scan", sr_file])


if __name__ == "__main__":
    main()
