"""E2E test: compare measured metrics against lqsim predictions.

Sends moderate load through the validation topology and checks that
the measured response time matches lqsim predictions within MAPE < 25%.

Prerequisites:
- Docker with GMT image built (gmt-test:latest)
- lqsim available (LQSIM_PATH or in PATH)
- Run: pytest tests/e2e/test_lqsim_predictions.py -v -s
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

# Add tools/ and src/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from lqn_compiler import build_task_config
from lqn_parser import parse_lqn_file
from lqsim_runner import find_lqsim, run_and_parse

MODEL_PATH = (
    Path(__file__).parent.parent.parent
    / "test"
    / "lqn-groundtruth"
    / "validation-model.lqn"
)
IMAGE = os.environ.get("GMT_E2E_IMAGE", "gmt-test:latest")
NETWORK = "gmt-e2e-lqsim"
TSERVER_PORT = 18082
TLEAF_PORT = 18083


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
) -> tuple[str, str]:
    """Build LQN_TASK_CONFIG JSON for TServer and TLeaf.

    Adjusts sync call URLs to use Docker container names with port 8080.
    """
    model = parse_lqn_file(str(model_path))
    tserver = next(t for t in model.tasks if t.name == "TServer")
    tleaf = next(t for t in model.tasks if t.name == "TLeaf")

    tserver_config = build_task_config(tserver, model)
    tleaf_config = build_task_config(tleaf, model)

    # Rewrite sync call URLs for Docker networking
    for act_data in tserver_config.get("activities", {}).values():
        if "sync_calls" in act_data:
            new_calls = {}
            for url, mean in act_data["sync_calls"].items():
                docker_url = url.replace("tleaf-svc", "tleaf-lqsim:8080")
                new_calls[docker_url] = mean
            act_data["sync_calls"] = new_calls

    return json.dumps(tserver_config), json.dumps(tleaf_config)


@pytest.fixture(scope="module")
def lqsim_predictions() -> dict[str, dict[str, float]]:
    """Run lqsim and return predicted metrics."""
    if not find_lqsim():
        pytest.skip("lqsim not available")
    return run_and_parse(str(MODEL_PATH))


@pytest.fixture(scope="module")
def docker_topology():
    """Start TServer + TLeaf Docker containers on a shared network."""
    if not _docker_available():
        pytest.skip("Docker not available")

    tserver_config, tleaf_config = _build_docker_configs(MODEL_PATH)

    # Cleanup any previous runs
    for name in ("tserver-lqsim", "tleaf-lqsim"):
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
                "docker",
                "run",
                "-d",
                "--name",
                "tleaf-lqsim",
                "--network",
                NETWORK,
                "-p",
                f"{TLEAF_PORT}:8080",
                "-e",
                "GUNICORN_WORKERS=1",
                "-e",
                f"LQN_TASK_CONFIG={tleaf_config}",
                "-e",
                "SERVICE_NAME=tleaf-lqsim",
                IMAGE,
            ]
        )

        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                "tserver-lqsim",
                "--network",
                NETWORK,
                "-p",
                f"{TSERVER_PORT}:8080",
                "-e",
                "GUNICORN_WORKERS=1",
                "-e",
                f"LQN_TASK_CONFIG={tserver_config}",
                "-e",
                "SERVICE_NAME=tserver-lqsim",
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
                ["docker", "logs", "tserver-lqsim"], capture_output=True, text=True
            )
            pytest.fail(
                f"TServer not ready after 30s. Logs:\n{logs.stdout}\n{logs.stderr}"
            )

        yield base_url

    finally:
        for name in ("tserver-lqsim", "tleaf-lqsim"):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=10
            )
        subprocess.run(
            ["docker", "network", "rm", NETWORK], capture_output=True, timeout=10
        )


@pytest.mark.e2e
class TestLqsimPredictions:
    """Moderate-load test: compare measured RT against lqsim predictions."""

    N_REQUESTS = 500
    REQUEST_RATE = 10  # req/s
    MAPE_THRESHOLD = 0.25  # 25%

    def test_response_time_mape(
        self,
        lqsim_predictions: dict[str, dict[str, float]],
        docker_topology: str,
    ) -> None:
        """Measured RT should match lqsim predicted RT within 25% MAPE."""
        base_url = docker_topology

        pred_rt = lqsim_predictions.get("TServer", {}).get("service_time")
        assert pred_rt is not None, "lqsim did not produce service_time for TServer"

        # Send requests at moderate rate
        delay = 1.0 / self.REQUEST_RATE
        response_times: list[float] = []

        for i in range(self.N_REQUESTS):
            start = time.monotonic()
            resp = requests.get(f"{base_url}/process", timeout=30)
            elapsed = time.monotonic() - start
            assert resp.status_code == 200, f"Request {i}: status {resp.status_code}"
            response_times.append(elapsed)
            # Pace requests to maintain target rate
            sleep_time = delay - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        rt_mean = sum(response_times) / len(response_times)
        mape = abs(pred_rt - rt_mean) / rt_mean

        print("\n--- lqsim Prediction Comparison ---")
        print(f"Requests:     {self.N_REQUESTS}")
        print(f"Target rate:  {self.REQUEST_RATE} req/s")
        print(f"RT predicted: {pred_rt:.6f}s")
        print(f"RT measured:  {rt_mean:.6f}s")
        print(f"MAPE:         {mape:.1%}")

        assert mape < self.MAPE_THRESHOLD, (
            f"MAPE {mape:.1%} exceeds threshold {self.MAPE_THRESHOLD:.0%}. "
            f"Predicted RT={pred_rt:.6f}, Measured RT={rt_mean:.6f}"
        )
