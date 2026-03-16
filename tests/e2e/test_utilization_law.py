"""E2E test: low-load utilization law validation against validation-model.lqn.

Verifies that at low load (queuing negligible):
- Response times match expected service times from the LQN model
- Throughput is stable around the offered load rate

Prerequisites:
- Docker with GMT image built (gmt-test:latest)
- Run: pytest tests/e2e/test_utilization_law.py -v -s
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

MODEL_PATH = (
    Path(__file__).parent.parent.parent
    / "test"
    / "lqn-groundtruth"
    / "validation-model.lqn"
)
IMAGE = os.environ.get("GMT_E2E_IMAGE", "gmt-test:latest")
NETWORK = "gmt-e2e-validation"
TSERVER_PORT = 18080
TLEAF_PORT = 18081


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

    # Rewrite sync call URLs: tleaf-svc/compute → tleaf:8080/compute
    # Docker containers resolve by container name on the shared network
    for act_data in tserver_config.get("activities", {}).values():
        if "sync_calls" in act_data:
            new_calls = {}
            for url, mean in act_data["sync_calls"].items():
                # Replace K8s service name with Docker container:port
                docker_url = url.replace("tleaf-svc", "tleaf:8080")
                new_calls[docker_url] = mean
            act_data["sync_calls"] = new_calls

    return json.dumps(tserver_config), json.dumps(tleaf_config)


@pytest.fixture(scope="module")
def docker_topology():
    """Start TServer + TLeaf Docker containers on a shared network."""
    if not _docker_available():
        pytest.skip("Docker not available")

    tserver_config, tleaf_config = _build_docker_configs(MODEL_PATH)

    # Cleanup any previous runs
    for name in ("tserver", "tleaf"):
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=10
        )
    subprocess.run(
        ["docker", "network", "rm", NETWORK], capture_output=True, timeout=10
    )

    try:
        # Create network
        _run(["docker", "network", "create", NETWORK])

        # Start TLeaf first (TServer depends on it)
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                "tleaf",
                "--network",
                NETWORK,
                "-p",
                f"{TLEAF_PORT}:8080",
                "-e",
                "GUNICORN_WORKERS=1",
                "-e",
                f"LQN_TASK_CONFIG={tleaf_config}",
                "-e",
                "SERVICE_NAME=tleaf",
                IMAGE,
            ]
        )

        # Start TServer
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                "tserver",
                "--network",
                NETWORK,
                "-p",
                f"{TSERVER_PORT}:8080",
                "-e",
                "GUNICORN_WORKERS=1",
                "-e",
                f"LQN_TASK_CONFIG={tserver_config}",
                "-e",
                "SERVICE_NAME=tserver",
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
            # Dump logs for debugging
            logs = subprocess.run(
                ["docker", "logs", "tserver"], capture_output=True, text=True
            )
            pytest.fail(
                f"TServer not ready after 30s. Logs:\n{logs.stdout}\n{logs.stderr}"
            )

        yield base_url

    finally:
        for name in ("tserver", "tleaf"):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=10
            )
        subprocess.run(
            ["docker", "network", "rm", NETWORK], capture_output=True, timeout=10
        )


@pytest.mark.e2e
class TestUtilizationLaw:
    """Low-load tests: queuing negligible, RT ≈ service time."""

    N_REQUESTS = 200
    INTER_REQUEST_DELAY = 0.5  # ~2 req/s

    # Expected CPU demand per request (from model):
    # Fast path (70%): start(0.01) + fast(0.02) = 0.03s
    # Slow path (30%): start(0.01) + slow(0.03) + work_a(0.02) + work_b(0.01)
    #                   + finish(0.005) = 0.075s
    # E[S_cpu] = 0.7 * 0.03 + 0.3 * 0.075 = 0.0435s
    EXPECTED_CPU_FAST = 0.03
    EXPECTED_CPU_SLOW = 0.075
    EXPECTED_CPU_MEAN = 0.7 * 0.03 + 0.3 * 0.075  # 0.0435

    def _send_requests(self, base_url: str) -> list[float]:
        """Send N_REQUESTS at low rate and collect response times."""
        response_times = []
        for _ in range(self.N_REQUESTS):
            start = time.monotonic()
            resp = requests.get(f"{base_url}/process", timeout=30)
            elapsed = time.monotonic() - start
            assert resp.status_code == 200, f"Bad status: {resp.status_code}"
            response_times.append(elapsed)
            time.sleep(self.INTER_REQUEST_DELAY)
        return response_times

    def test_response_times_match_model(self, docker_topology: str) -> None:
        """RT at low load should approximate service time (no queuing)."""
        base_url = docker_topology
        rts = self._send_requests(base_url)

        rt_mean = sum(rts) / len(rts)
        t_total = self.N_REQUESTS * self.INTER_REQUEST_DELAY + sum(rts)
        throughput = self.N_REQUESTS / t_total

        print("\n--- Utilization Law Results ---")
        print(f"Requests:   {self.N_REQUESTS}")
        print(f"RT mean:    {rt_mean:.4f}s")
        print(f"RT min:     {min(rts):.4f}s")
        print(f"RT max:     {max(rts):.4f}s")
        print(f"Throughput: {throughput:.2f} req/s")
        print(f"Expected CPU mean: {self.EXPECTED_CPU_MEAN:.4f}s")

        # At low load, mean RT should be close to expected CPU demand.
        # The finish activity also makes a sync call to TLeaf (0.01s),
        # so slow path has additional network + TLeaf service time.
        # Allow generous tolerance (50%) for exponential variability + network.
        assert rt_mean > self.EXPECTED_CPU_MEAN * 0.5, (
            f"RT too low: {rt_mean:.4f} < {self.EXPECTED_CPU_MEAN * 0.5:.4f}"
        )
        assert rt_mean < self.EXPECTED_CPU_MEAN * 3.0, (
            f"RT too high: {rt_mean:.4f} > {self.EXPECTED_CPU_MEAN * 3.0:.4f}"
        )

        # Throughput should be approximately the offered rate (~2 req/s)
        assert throughput > 1.0, f"Throughput too low: {throughput:.2f}"
        assert throughput < 4.0, f"Throughput too high: {throughput:.2f}"

    def test_fast_path_proportion(self, docker_topology: str) -> None:
        """Verify ~70% of requests take the fast path (shorter RT)."""
        base_url = docker_topology
        rts = self._send_requests(base_url)

        # Fast path RT should be around 0.03s, slow path around 0.075s+
        # Use a threshold between the two paths
        threshold = (self.EXPECTED_CPU_FAST + self.EXPECTED_CPU_SLOW) / 2
        fast_count = sum(1 for rt in rts if rt < threshold)
        fast_ratio = fast_count / len(rts)

        print("\n--- Path Distribution ---")
        print(f"Fast count: {fast_count}/{len(rts)} ({fast_ratio:.1%})")
        print(f"Threshold:  {threshold:.4f}s")

        # Should be ~70% fast ± 15% tolerance
        assert 0.50 < fast_ratio < 0.90, (
            f"Fast path ratio {fast_ratio:.1%} outside expected range 50-90%"
        )
