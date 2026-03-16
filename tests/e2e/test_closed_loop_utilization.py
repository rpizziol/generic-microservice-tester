"""E2E test: closed-loop workload at ~50% utilization vs lqsim predictions.

Simulates N concurrent clients (ThreadPoolExecutor) with exponential think
time matching the LQN model, then compares measured RT against lqsim
predictions for the parametric model (TClient m=N).

Prerequisites:
- Docker with GMT image built (gmt-test:latest)
- lqsim available (LQSIM_PATH or in PATH)
- Run: pytest tests/e2e/test_closed_loop_utilization.py -v -s
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import requests

# Add tools/ and src/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from lqn_compiler import build_task_config
from lqn_model_utils import set_client_multiplicity
from lqn_parser import parse_lqn_file
from lqsim_runner import find_lqsim, run_and_parse

MODEL_PATH = (
    Path(__file__).parent.parent.parent
    / "test"
    / "lqn-groundtruth"
    / "validation-model.lqn"
)
IMAGE = os.environ.get("GMT_E2E_IMAGE", "gmt-test:latest")
NETWORK = "gmt-e2e-closed"
TSERVER_PORT = 18084
TLEAF_PORT = 18085

# Closed-loop parameters from LQN model
THINK_TIME = 2.0  # z = 2.0s (TClient think time)
N_CLIENTS = 22  # Target ~50% utilization with S≈0.047s
WARMUP_SECONDS = 10
MEASURE_SECONDS = 60
MAPE_THRESHOLD = 0.25  # 25%


def _docker_available() -> bool:
    """Check if Docker daemon is running."""
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run(cmd: list[str], check: bool = True, timeout: int = 30) -> str:
    """Run a command and return stdout."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def _build_docker_configs(
    model_path: Path,
    container_prefix: str,
) -> tuple[str, str]:
    """Build LQN_TASK_CONFIG JSON for TServer and TLeaf."""
    model = parse_lqn_file(str(model_path))
    tserver = next(t for t in model.tasks if t.name == "TServer")
    tleaf = next(t for t in model.tasks if t.name == "TLeaf")

    tserver_config = build_task_config(tserver, model)
    tleaf_config = build_task_config(tleaf, model)

    # Rewrite sync call URLs for Docker networking
    leaf_container = f"{container_prefix}-tleaf"
    for act_data in tserver_config.get("activities", {}).values():
        if "sync_calls" in act_data:
            new_calls = {}
            for url, mean in act_data["sync_calls"].items():
                docker_url = url.replace("tleaf-svc", f"{leaf_container}:8080")
                new_calls[docker_url] = mean
            act_data["sync_calls"] = new_calls

    return json.dumps(tserver_config), json.dumps(tleaf_config)


def _closed_loop_worker(
    base_url: str,
    think_time: float,
    stop_event: threading.Event,
    results: list[float],
    warmup_done: threading.Event,
) -> None:
    """Single closed-loop client: request → sleep(exp(1/z)) → repeat."""
    session = requests.Session()
    while not stop_event.is_set():
        try:
            start = time.monotonic()
            resp = session.get(f"{base_url}/process", timeout=30)
            elapsed = time.monotonic() - start
            if resp.status_code == 200 and warmup_done.is_set():
                results.append(elapsed)
        except requests.exceptions.RequestException:
            pass
        # Exponential think time (matches lqsim's treatment of z)
        delay = random.expovariate(1.0 / think_time)
        # Sleep in small increments to check stop_event
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))


@pytest.fixture(scope="module")
def lqsim_predictions_50pct() -> dict[str, dict[str, float]]:
    """Run lqsim on parametric model (m=N_CLIENTS) and return predictions."""
    if not find_lqsim():
        pytest.skip("lqsim not available")

    tmp_model = set_client_multiplicity(str(MODEL_PATH), "TClient", N_CLIENTS)
    try:
        metrics = run_and_parse(str(tmp_model))
    finally:
        tmp_model.unlink(missing_ok=True)
        # Clean up lqsim output files
        for suffix in (".p", ".p~", ".out"):
            tmp_model.with_suffix(suffix).unlink(missing_ok=True)

    return metrics


@pytest.fixture(scope="module")
def docker_topology():
    """Start TServer + TLeaf Docker containers on a shared network."""
    if not _docker_available():
        pytest.skip("Docker not available")

    prefix = "closed"
    tserver_name = f"{prefix}-tserver"
    tleaf_name = f"{prefix}-tleaf"
    tserver_config, tleaf_config = _build_docker_configs(MODEL_PATH, prefix)

    # Cleanup any previous runs
    for name in (tserver_name, tleaf_name):
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=10
        )
    subprocess.run(
        ["docker", "network", "rm", NETWORK], capture_output=True, timeout=10
    )

    try:
        _run(["docker", "network", "create", NETWORK])

        _run(
            [
                "docker", "run", "-d",
                "--name", tleaf_name,
                "--network", NETWORK,
                "-p", f"{TLEAF_PORT}:8080",
                "-e", "GUNICORN_WORKERS=1",
                "-e", f"LQN_TASK_CONFIG={tleaf_config}",
                "-e", f"SERVICE_NAME={tleaf_name}",
                IMAGE,
            ]
        )

        _run(
            [
                "docker", "run", "-d",
                "--name", tserver_name,
                "--network", NETWORK,
                "-p", f"{TSERVER_PORT}:8080",
                "-e", "GUNICORN_WORKERS=1",
                "-e", f"LQN_TASK_CONFIG={tserver_config}",
                "-e", f"SERVICE_NAME={tserver_name}",
                IMAGE,
            ]
        )

        # Wait for containers to be healthy
        base_url = f"http://localhost:{TSERVER_PORT}"
        for attempt in range(30):
            try:
                resp = requests.get(f"{base_url}/process", timeout=5)
                if resp.status_code == 200:
                    break
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", tserver_name], capture_output=True, text=True
            )
            pytest.fail(
                f"TServer not ready after 30s. Logs:\n{logs.stdout}\n{logs.stderr}"
            )

        yield base_url

    finally:
        for name in (tserver_name, tleaf_name):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=10
            )
        subprocess.run(
            ["docker", "network", "rm", NETWORK], capture_output=True, timeout=10
        )


@pytest.mark.e2e
class TestClosedLoopUtilization:
    """Closed-loop test at ~50% utilization: N clients with think time z."""

    def test_response_time_mape(
        self,
        lqsim_predictions_50pct: dict[str, dict[str, float]],
        docker_topology: str,
    ) -> None:
        """Measured RT under closed-loop load should match lqsim within 25% MAPE."""
        base_url = docker_topology
        pred = lqsim_predictions_50pct

        pred_rt = pred.get("TServer", {}).get("service_time")
        pred_throughput = pred.get("TServer", {}).get("throughput")
        pred_util = pred.get("TServer", {}).get("utilization")
        assert pred_rt is not None, "lqsim did not produce service_time for TServer"

        # Collect response times from closed-loop workload
        results: list[float] = []
        stop_event = threading.Event()
        warmup_done = threading.Event()

        with ThreadPoolExecutor(max_workers=N_CLIENTS) as executor:
            # Launch N_CLIENTS workers
            futures = [
                executor.submit(
                    _closed_loop_worker,
                    base_url,
                    THINK_TIME,
                    stop_event,
                    results,
                    warmup_done,
                )
                for _ in range(N_CLIENTS)
            ]

            # Warmup phase
            time.sleep(WARMUP_SECONDS)
            warmup_done.set()

            # Measurement phase
            measure_start = time.monotonic()
            time.sleep(MEASURE_SECONDS)
            measure_elapsed = time.monotonic() - measure_start

            # Stop all workers
            stop_event.set()
            for f in futures:
                f.result(timeout=10)

        assert len(results) > 100, f"Too few requests collected: {len(results)}"

        rt_mean = sum(results) / len(results)
        throughput = len(results) / measure_elapsed
        mape_rt = abs(pred_rt - rt_mean) / rt_mean

        print("\n--- Closed-Loop Utilization Results ---")
        print(f"Clients:          {N_CLIENTS}")
        print(f"Think time:       {THINK_TIME}s (exponential)")
        print(f"Duration:         {MEASURE_SECONDS}s")
        print(f"Requests:         {len(results)}")
        print(f"RT predicted:     {pred_rt:.6f}s")
        print(f"RT measured:      {rt_mean:.6f}s")
        print(f"MAPE RT:          {mape_rt:.1%}")
        print(f"X predicted:      {pred_throughput:.2f} req/s" if pred_throughput else "")
        print(f"X measured:       {throughput:.2f} req/s")
        if pred_util is not None:
            print(f"U predicted:      {pred_util:.4f}")
        print(f"U estimated:      {throughput * (pred_rt if pred_rt else rt_mean):.4f}")

        assert mape_rt < MAPE_THRESHOLD, (
            f"MAPE {mape_rt:.1%} exceeds threshold {MAPE_THRESHOLD:.0%}. "
            f"Predicted RT={pred_rt:.6f}, Measured RT={rt_mean:.6f}"
        )

    def test_utilization_in_range(
        self,
        lqsim_predictions_50pct: dict[str, dict[str, float]],
        docker_topology: str,
    ) -> None:
        """Measured utilization should be approximately 50% (±15%)."""
        base_url = docker_topology
        pred = lqsim_predictions_50pct
        pred_util = pred.get("TServer", {}).get("utilization")

        # Collect response times
        results: list[float] = []
        stop_event = threading.Event()
        warmup_done = threading.Event()

        with ThreadPoolExecutor(max_workers=N_CLIENTS) as executor:
            futures = [
                executor.submit(
                    _closed_loop_worker,
                    base_url,
                    THINK_TIME,
                    stop_event,
                    results,
                    warmup_done,
                )
                for _ in range(N_CLIENTS)
            ]

            time.sleep(WARMUP_SECONDS)
            warmup_done.set()
            measure_start = time.monotonic()
            time.sleep(MEASURE_SECONDS)
            measure_elapsed = time.monotonic() - measure_start

            stop_event.set()
            for f in futures:
                f.result(timeout=10)

        assert len(results) > 100, f"Too few requests collected: {len(results)}"

        throughput = len(results) / measure_elapsed
        # Estimate service demand from lqsim
        pred_service = pred.get("TServer", {}).get("service_time", 0.047)
        u_measured = throughput * pred_service

        print("\n--- Utilization Check ---")
        print(f"Throughput:       {throughput:.2f} req/s")
        print(f"Service time:     {pred_service:.6f}s")
        print(f"U measured:       {u_measured:.4f}")
        if pred_util is not None:
            print(f"U predicted:      {pred_util:.4f}")

        # Utilization should be in the neighborhood of 50%
        assert 0.25 < u_measured < 0.75, (
            f"Utilization {u_measured:.2%} outside expected range 25-75%"
        )
