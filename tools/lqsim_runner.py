#!/usr/bin/env python3
"""Minimal wrapper around lqsim for running LQN simulations and parsing results.

Usage:
    python tools/lqsim_runner.py model.lqn
    LQSIM_PATH=/opt/lqns/bin/lqsim python tools/lqsim_runner.py model.lqn
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_lqsim() -> str | None:
    """Find lqsim binary via LQSIM_PATH env var or PATH lookup."""
    env_path = os.environ.get("LQSIM_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    return shutil.which("lqsim")


def run_lqsim(
    model_path: str,
    *,
    confidence: str = "5,10,1000000",
) -> Path:
    """Run lqsim on a model and return path to the .p output file.

    Args:
        model_path: Path to .lqn model file.
        confidence: Confidence interval spec (level%, precision%, max_blocks).

    Returns:
        Path to the generated .p file.

    Raises:
        FileNotFoundError: If lqsim binary not found.
        RuntimeError: If lqsim exits with non-zero status.
    """
    lqsim = find_lqsim()
    if not lqsim:
        raise FileNotFoundError(
            "lqsim not found. Set LQSIM_PATH or add lqsim to PATH."
        )

    model = Path(model_path).resolve()
    if not model.exists():
        raise FileNotFoundError(f"Model file not found: {model}")

    cmd = [lqsim, "-p", "-C", confidence, str(model)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(
            f"lqsim failed (exit {result.returncode}):\n{result.stderr}"
        )

    # lqsim writes .p file in the same directory as the model
    p_file = model.with_suffix(".p")
    if not p_file.exists():
        raise RuntimeError(f"Expected output file not found: {p_file}")

    return p_file


def parse_p_file(p_file_path: str | Path) -> dict[str, dict[str, float]]:
    """Parse lqsim --parseable (.p) output file into structured metrics.

    The .p format uses tabular sections. We extract:
    - throughput: from "Throughputs and utilizations per phase" section
    - service_time: from "Service times" section
    - utilization: from "Utilization and waiting per phase for processor" sections

    Returns:
        Dict keyed by task name, each containing metrics like
        throughput, service_time, utilization.
    """
    p_file = Path(p_file_path)
    content = p_file.read_text()
    lines = content.splitlines()

    metrics: dict[str, dict[str, float]] = {}

    def _find_section(header: str) -> int:
        """Find the line index of a section header."""
        for i, line in enumerate(lines):
            if header in line:
                return i
        return -1

    def _parse_float(s: str) -> float | None:
        try:
            return float(s)
        except ValueError:
            return None

    # Parse "Service times:" section
    # Format: TaskName  EntryName  Phase1Value
    idx = _find_section("Service times:")
    if idx >= 0:
        current_task = None
        for line in lines[idx + 3 :]:  # skip header + column headers + blank
            if not line.strip() or line.startswith("+/-"):
                if not line.strip():
                    break
                continue
            parts = line.split()
            if not parts:
                break
            # Lines starting with +/- are confidence intervals — skip
            if parts[0].startswith("+/-"):
                continue
            # Task name starts at column 0 (not indented)
            if not line.startswith(" "):
                current_task = parts[0]
                # Entry or activity name is next, value after
                if len(parts) >= 3:
                    val = _parse_float(parts[-1])
                    if val is not None and current_task:
                        metrics.setdefault(current_task, {})["service_time"] = val
            else:
                # Indented: continuation (activity or +/- line)
                if parts[0].startswith("+/-"):
                    continue
                # Could be "Activity Name" header — skip
                if parts[0] == "Activity":
                    continue
                # Activity-level service time
                if len(parts) >= 2 and current_task:
                    val = _parse_float(parts[-1])
                    if val is not None:
                        metrics.setdefault(current_task, {})[
                            f"service_time_{parts[0]}"
                        ] = val

    # Parse "Throughputs and utilizations per phase:" section
    # Format: TaskName  EntryName  Throughput  Phase1  Total
    idx = _find_section("Throughputs and utilizations per phase:")
    if idx >= 0:
        current_task = None
        for line in lines[idx + 3 :]:
            if not line.strip():
                break
            parts = line.split()
            if not parts or parts[0].startswith("+/-"):
                continue
            if not line.startswith(" "):
                current_task = parts[0]
                if len(parts) >= 3:
                    val = _parse_float(parts[2])
                    if val is not None and current_task:
                        metrics.setdefault(current_task, {})["throughput"] = val
            else:
                if parts[0].startswith("+/-") or parts[0] == "Activity":
                    continue

    # Parse "Utilization and waiting per phase for processor:" sections
    # Multiple sections, one per processor.
    # Format after header: blank line, column headers, then data lines.
    for match in re.finditer(
        r"Utilization and waiting per phase for processor:\s+(\S+)", content
    ):
        proc_name = match.group(1)
        start = content.index(match.group(0))
        section_start = content.index("\n", start) + 1
        section_lines = content[section_start:].splitlines()

        current_task = None
        data_started = False
        blank_count = 0
        for line in section_lines:
            parts = line.split()
            # Skip blank lines before data starts
            if not line.strip():
                if data_started:
                    blank_count += 1
                    if blank_count >= 2:
                        break
                continue
            blank_count = 0
            if not parts or parts[0].startswith("+/-"):
                continue
            # Skip column header line
            if parts[0] == "Task":
                continue
            data_started = True
            if not line.startswith(" "):
                current_task = parts[0]
                # Format: TaskName Pri N EntryName Utilization Phase1
                if len(parts) >= 5:
                    val = _parse_float(parts[4])
                    if val is not None and current_task:
                        metrics.setdefault(current_task, {})["utilization"] = val
                        metrics[current_task]["processor"] = proc_name
            else:
                if parts[0].startswith("+/-") or parts[0] == "Activity":
                    continue

    return metrics


def run_and_parse(
    model_path: str,
    *,
    confidence: str = "5,10,1000000",
) -> dict[str, dict[str, float]]:
    """Run lqsim on a model and return parsed metrics."""
    p_file = run_lqsim(model_path, confidence=confidence)
    return parse_p_file(p_file)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <model.lqn>", file=sys.stderr)
        sys.exit(1)

    model_path = sys.argv[1]
    try:
        metrics = run_and_parse(model_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    for name, data in sorted(metrics.items()):
        print(f"\n{name}:")
        for key, value in sorted(data.items()):
            if isinstance(value, float):
                print(f"  {key}: {value:.6f}")
            else:
                print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
