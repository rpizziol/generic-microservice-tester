import ctypes
import json
import math
import os
import pathlib
import random
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

import numpy as np
import requests
from flask import Flask, jsonify
from requests.adapters import HTTPAdapter

app = Flask(__name__)

# --- Worker-Isolated Resource Management ---
# Each Gunicorn worker gets its own isolated resources to prevent cross-worker interference

# Main session for synchronous calls (per worker)
SESSION = requests.Session()
adapter = HTTPAdapter(
    pool_connections=100, pool_maxsize=100
)  # Large pool for sync calls
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

# Dedicated session for asynchronous calls (completely isolated)
ASYNC_SESSION = requests.Session()
async_adapter = HTTPAdapter(
    pool_connections=100,  # Large pool for async calls
    pool_maxsize=100,
    max_retries=0,  # No retries for pure async semantics
)
ASYNC_SESSION.mount("http://", async_adapter)
ASYNC_SESSION.mount("https://", async_adapter)

# Worker-isolated thread pool for async calls
# Increased capacity for better throughput while maintaining worker isolation
ASYNC_EXECUTOR = ThreadPoolExecutor(
    max_workers=10,  # More threads per worker for higher async throughput
    thread_name_prefix=f"async-worker-{os.getpid()}",
)


# --- Configuration Section ---
# The behavior of this microservice is controlled by environment variables.
#
# SERVICE_NAME: A friendly name for this instance (e.g., "frontend", "backend-a").
#
# SERVICE_TIME_SECONDS: Simulates a precise amount of CPU time consumption using a busy-wait.
#                       Example: "0.1" for 100ms of CPU time.
#
# OUTBOUND_CALLS: Defines downstream HTTP calls.
#                 Format: "TYPE:service_name:probability,TYPE:service_name:probability,..."
#                 Example: "SYNC:backend-a:0.6,SYNC:backend-b:0.4,ASYNC:logger-svc:1.0"

# --- Process-based CPU Timing with Delta Tracking ---
# Global state for tracking CPU time between requests within the same worker process
_last_user_time = 0.0


def do_work():
    """Simulates CPU-intensive work using precise per-request user CPU time tracking.

    Uses delta tracking to measure only the CPU time consumed by this specific request,
    accounting for the fact that worker processes are persistent and accumulate CPU time
    across multiple requests.

    This implementation:
    - Measures only user-space CPU time (excludes system time)
    - Uses delta tracking to isolate per-request CPU consumption
    - Handles worker process restarts automatically
    - Implements skip strategy when inherited CPU time exceeds target
    """
    global _last_user_time

    try:
        import psutil

        service_time_str = os.environ.get("SERVICE_TIME_SECONDS", "0")
        mean_service_time = float(service_time_str)

        if mean_service_time <= 0:
            return

        # Sample from exponential distribution with specified mean
        service_time = np.random.exponential(mean_service_time)

        # Get current user CPU time of the worker process
        process = psutil.Process()
        current_user = process.cpu_times().user

        # Worker process restart detection
        if current_user < _last_user_time:
            print(
                f"Worker restart detected: current={current_user:.4f}s, last={_last_user_time:.4f}s"
            )
            _last_user_time = 0.0

        # Calculate inherited CPU time from previous requests in this worker
        inherited_user = current_user - _last_user_time

        # How much additional work do we need to do for this request?
        remaining = service_time - inherited_user

        print(
            f"CPU timing: mean={mean_service_time:.4f}s, sampled={service_time:.4f}s, inherited={inherited_user:.4f}s, remaining={remaining:.4f}s"
        )

        if remaining > 0:
            # Standard busy-wait for remaining CPU time
            start_busy = time.process_time()
            while (time.process_time() - start_busy) < remaining:
                pass  # Busy computation loop

            print(f"Completed busy-wait for {remaining:.4f}s")
        else:
            # Skip strategy: request completed without additional work
            print(
                f"Request completed without additional work (excess: {abs(remaining):.4f}s)"
            )

        # Update tracking for next request
        _last_user_time = process.cpu_times().user

    except ImportError as e:
        raise RuntimeError(
            "psutil library not available - required for container-aware CPU timing"
        ) from e
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            f"Invalid SERVICE_TIME_SECONDS value: {service_time_str}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Process-based CPU timing failed: {e}") from e


def parse_outbound_calls():
    """Reads and parses the OUTBOUND_CALLS environment variable."""
    config_str = os.environ.get("OUTBOUND_CALLS", "")
    if not config_str:
        return [], []

    targets = []
    call_defs = config_str.split(",")
    for call_def in call_defs:
        try:
            call_type, service_name, prob_str = call_def.strip().split(":")
            probability = float(prob_str)
            targets.append(
                {
                    "type": call_type.upper(),
                    "service": service_name,
                    "probability": probability,
                }
            )
        except ValueError:
            print(f"Skipping malformed call definition: {call_def}")

    # Separate probabilistic calls from fixed calls (which are always executed)
    probabilistic_targets = [t for t in targets if t["probability"] < 1.0]
    fixed_targets = [t for t in targets if t["probability"] >= 1.0]

    return probabilistic_targets, fixed_targets


def make_call(target):
    """Executes a single HTTP GET request using the shared Session object."""
    service_name = target["service"]
    # Kubernetes DNS resolves the service name to its internal IP address
    url = f"http://{service_name}"
    try:
        # Use the shared SESSION object to make the request
        response = SESSION.get(url, timeout=300)
        print(f"Called {url}, status: {response.status_code}")
        return {"service": service_name, "status": response.status_code}
    except requests.exceptions.RequestException as e:
        print(f"Failed to call {url}: {e}")
        return {"service": service_name, "status": "error", "reason": str(e)}


def make_async_call_pooled(target):
    """Executes asynchronous HTTP calls using worker-isolated thread pool.

    Implements LQN-semantic compliant "send-no-reply" behavior.
    """
    service_name = target["service"]
    url = f"http://{service_name}"

    def _async_worker():
        """Worker function executed in isolated thread"""
        try:
            response = ASYNC_SESSION.get(url, timeout=300)
            print(f"[ASYNC-POOL-{os.getpid()}] {url} -> {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"[ASYNC-POOL-{os.getpid()}] {url} -> ERROR: {e}")
        except Exception as e:
            print(f"[ASYNC-POOL-{os.getpid()}] {url} -> UNEXPECTED: {e}")

    try:
        ASYNC_EXECUTOR.submit(_async_worker)
        print(f"Async call submitted to worker-{os.getpid()} pool: {url}")
    except Exception as e:
        print(f"Failed to submit async call to pool: {url} -> {e}")


# --- LQN Activity Engine ---
# When LQN_TASK_CONFIG is set, the microservice interprets an LQN task fragment
# with activity diagrams, AND-fork/join, OR-fork, and reply semantics.

# Load C extension for GIL-releasing busy-wait (for AND-fork parallelism)
_BUSY_WAIT_LIB = None


def _get_busy_wait_lib():
    """Lazily load the busy_wait shared library."""
    global _BUSY_WAIT_LIB
    if _BUSY_WAIT_LIB is not None:
        return _BUSY_WAIT_LIB

    # Look in current dir (Docker /app/) and src/ (local dev)
    for candidate in [
        pathlib.Path("busy_wait.so"),
        pathlib.Path(__file__).parent / "busy_wait.so",
    ]:
        if candidate.exists():
            _BUSY_WAIT_LIB = ctypes.CDLL(str(candidate.resolve()))
            _BUSY_WAIT_LIB.busy_wait_cpu.argtypes = [ctypes.c_double]
            _BUSY_WAIT_LIB.busy_wait_cpu.restype = None
            return _BUSY_WAIT_LIB
    return None


# Worker-isolated thread pool for AND-fork parallel execution
FORK_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix=f"fork-worker-{os.getpid()}",
)

# Cached LQN task config (parsed once per worker at first request)
_LQN_TASK_CONFIG = None
_LQN_CONFIG_LOADED = False


def _is_dry_run() -> bool:
    """Check if dry-run mode is active (no CPU work, no HTTP calls)."""
    return os.environ.get("LQN_DRY_RUN", "0") == "1"


def load_task_config() -> dict | None:
    """Load and cache LQN_TASK_CONFIG from env var. Returns None if not set."""
    global _LQN_TASK_CONFIG, _LQN_CONFIG_LOADED
    if _LQN_CONFIG_LOADED:
        return _LQN_TASK_CONFIG
    _LQN_CONFIG_LOADED = True

    config_str = os.environ.get("LQN_TASK_CONFIG", "")
    if not config_str:
        return None

    try:
        _LQN_TASK_CONFIG = json.loads(config_str)
        print(f"[LQN] Loaded task config: {_LQN_TASK_CONFIG.get('task_name', '?')}")
        return _LQN_TASK_CONFIG
    except json.JSONDecodeError as e:
        print(f"[LQN] ERROR: Invalid LQN_TASK_CONFIG JSON: {e}")
        return None


def do_busy_wait(service_time_mean: float, dry_run: bool = False) -> float:
    """Execute CPU busy-wait for a sampled service time.

    Uses the C extension (GIL-releasing) if available, otherwise falls back
    to the Python busy-wait loop. In dry-run mode, skips actual CPU work.

    Returns the actual sampled service time.
    """
    if service_time_mean <= 0:
        return 0.0

    sampled = np.random.exponential(service_time_mean)

    if dry_run:
        return sampled

    lib = _get_busy_wait_lib()
    if lib is not None:
        lib.busy_wait_cpu(sampled)
    else:
        start = time.process_time()
        while (time.process_time() - start) < sampled:
            pass

    return sampled


def execute_mean_calls(
    url: str,
    mean_calls: float,
    call_type: str,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Execute N HTTP calls where N is derived from mean_calls.

    For integer part: always execute that many calls.
    For fractional part: probabilistic extra call.
    """
    n_calls = math.floor(mean_calls)
    fractional = mean_calls - n_calls
    if fractional > 0 and random.random() < fractional:
        n_calls += 1

    results = []
    for _ in range(n_calls):
        if call_type == "SYNC":
            if trace is not None:
                trace.append({"type": "sync_call", "target": url})
            if dry_run:
                results.append({"service": url, "status": "dry_run"})
            else:
                results.append(make_call({"service": url}))
        elif call_type == "ASYNC":
            if trace is not None:
                trace.append({"type": "async_call", "target": url})
            if not dry_run:
                make_async_call_pooled({"service": url})
            results.append({"service": url, "status": "async_pooled"})
    return results


def execute_activity(
    activity_name: str,
    config: dict,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Execute a single LQN activity: service time + outbound calls."""
    activities = config.get("activities", {})
    act_def = activities.get(activity_name, {})
    results = []

    st = act_def.get("service_time", 0.0)
    ts_start = time.monotonic()
    sampled = do_busy_wait(st, dry_run=dry_run) if st > 0 else 0.0
    ts_end = time.monotonic()

    if trace is not None:
        trace.append(
            {
                "type": "activity",
                "name": activity_name,
                "service_time_mean": st,
                "service_time_sampled": sampled,
                "timestamp_start": ts_start,
                "timestamp_end": ts_end,
            }
        )

    if st > 0 and not dry_run:
        print(f"[LQN] Activity {activity_name}: service_time={sampled:.4f}s")

    for target_url, mean_calls in (act_def.get("sync_calls") or {}).items():
        results.extend(
            execute_mean_calls(target_url, mean_calls, "SYNC", trace, dry_run)
        )

    for target_url, mean_calls in (act_def.get("async_calls") or {}).items():
        results.extend(
            execute_mean_calls(target_url, mean_calls, "ASYNC", trace, dry_run)
        )

    return results


def execute_and_fork(
    branches: list[str],
    config: dict,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Execute AND-fork branches in parallel using ThreadPoolExecutor + C extension.

    Each branch runs in a separate thread. The C busy-wait releases the GIL,
    enabling true CPU parallelism. Wall-clock time ~= max(branch times).

    Thread safety: each branch gets its own sub-trace, merged with branch tags
    at the join point.
    """
    if trace is not None:
        trace.append({"type": "and_fork", "branches": list(branches)})

    if dry_run:
        # Sequential execution in dry-run — deterministic, no threading
        # Tag events with "branch" for parity with real parallel execution
        results = []
        for branch_name in branches:
            branch_trace = [] if trace is not None else None
            results.extend(execute_activity(branch_name, config, branch_trace, dry_run))
            if trace is not None and branch_trace is not None:
                for event in branch_trace:
                    event["branch"] = branch_name
                    trace.append(event)
        return results

    # Real execution: parallel threads with per-branch sub-traces
    branch_traces: list[list[dict]] = [[] for _ in branches]
    futures = []
    for i, branch_name in enumerate(branches):
        future = FORK_EXECUTOR.submit(
            execute_activity, branch_name, config, branch_traces[i], dry_run
        )
        futures.append(future)

    futures_wait(futures)

    # Merge branch traces into main trace with branch tags
    if trace is not None:
        for i, bt in enumerate(branch_traces):
            for event in bt:
                event["branch"] = branches[i]
                trace.append(event)

    results = []
    for f in futures:
        results.extend(f.result())
    return results


def execute_or_fork(
    source: str,
    branches: list[dict],
    config: dict,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> tuple[str, list[dict]]:
    """Execute OR-fork: choose one branch probabilistically.

    Returns (chosen_activity_name, results).
    """
    names = [b["to"] for b in branches]
    weights = [b["prob"] for b in branches]
    chosen = random.choices(names, weights=weights, k=1)[0]

    if trace is not None:
        trace.append(
            {
                "type": "or_fork",
                "from": source,
                "chosen": chosen,
                "branches": names,
            }
        )

    results = execute_activity(chosen, config, trace, dry_run)
    return chosen, results


def execute_activity_graph(
    entry_name: str,
    config: dict,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Execute the activity graph for an entry, handling fork/join/choice/reply.

    Walks the graph from the entry's start_activity, following sequences,
    AND-forks, OR-forks, and stopping at reply points.

    If trace is not None, appends structured events for each step.
    If dry_run, skips CPU work and HTTP calls.
    """
    entries = config.get("entries", {})
    entry_def = entries.get(entry_name, {})
    graph = config.get("graph", {})

    start_activity = entry_def.get("start_activity")
    if not start_activity:
        return execute_phase_entry(entry_name, entry_def, trace, dry_run)

    # Build lookup structures for efficient graph traversal
    and_forks = {f["from"]: f["branches"] for f in graph.get("and_forks", [])}
    and_joins = {
        tuple(sorted(j["branches"])): j["to"] for j in graph.get("and_joins", [])
    }
    or_forks = {f["from"]: f["branches"] for f in graph.get("or_forks", [])}
    sequences = {}
    for a, b in graph.get("sequences", []):
        sequences[a] = b
    replies = graph.get("replies", {})

    activities = config.get("activities", {})
    results = []
    current = start_activity
    visited = set()
    max_iterations = 100

    while current:
        # Cycle detection
        if current in visited:
            raise RuntimeError(
                f"Cycle detected in activity graph: '{current}' visited twice"
            )
        visited.add(current)
        max_iterations -= 1
        if max_iterations <= 0:
            raise RuntimeError("Activity graph exceeded max iterations (100)")

        # Validate activity name exists
        if current not in activities:
            print(f"[LQN] WARNING: activity '{current}' not in activities config")

        # Check if this activity is a reply point for our entry
        if current in replies and replies[current] == entry_name:
            results.extend(execute_activity(current, config, trace, dry_run))
            if trace is not None:
                trace.append(
                    {"type": "reply", "activity": current, "entry": entry_name}
                )
            break

        # Check for AND-fork
        if current in and_forks:
            results.extend(execute_activity(current, config, trace, dry_run))
            fork_branches = and_forks[current]
            results.extend(execute_and_fork(fork_branches, config, trace, dry_run))
            join_key = tuple(sorted(fork_branches))
            if join_key in and_joins:
                if trace is not None:
                    trace.append(
                        {
                            "type": "and_join",
                            "branches": list(fork_branches),
                            "to": and_joins[join_key],
                        }
                    )
                current = and_joins[join_key]
            else:
                print(
                    f"[LQN] WARNING: no matching AND-join for branches {fork_branches}"
                )
                break
            continue

        # Check for OR-fork
        if current in or_forks:
            results.extend(execute_activity(current, config, trace, dry_run))
            chosen, branch_results = execute_or_fork(
                current, or_forks[current], config, trace, dry_run
            )
            results.extend(branch_results)
            if chosen in replies and replies[chosen] == entry_name:
                if trace is not None:
                    trace.append(
                        {"type": "reply", "activity": chosen, "entry": entry_name}
                    )
                break
            current = sequences.get(chosen)
            continue

        # Regular activity: execute and follow sequence
        results.extend(execute_activity(current, config, trace, dry_run))

        if current in sequences:
            current = sequences[current]
        else:
            break

    return results


def execute_phase_entry(
    entry_name: str,
    entry_def: dict,
    trace: list[dict] | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Execute a phase-based entry (no activity diagram)."""
    results = []

    st = entry_def.get("service_time", 0.0)
    sampled = do_busy_wait(st, dry_run=dry_run) if st > 0 else 0.0

    if trace is not None:
        trace.append(
            {
                "type": "phase_entry",
                "name": entry_name,
                "service_time_mean": st,
                "service_time_sampled": sampled,
            }
        )

    if st > 0 and not dry_run:
        print(f"[LQN] Phase entry {entry_name}: service_time={sampled:.4f}s")

    for target_url, mean_calls in (entry_def.get("sync_calls") or {}).items():
        results.extend(
            execute_mean_calls(target_url, mean_calls, "SYNC", trace, dry_run)
        )

    for target_url, mean_calls in (entry_def.get("async_calls") or {}).items():
        results.extend(
            execute_mean_calls(target_url, mean_calls, "ASYNC", trace, dry_run)
        )

    if trace is not None:
        trace.append({"type": "reply", "activity": entry_name, "entry": entry_name})

    return results


# --- Route Handlers ---


@app.route("/")
@app.route("/<entry_name>")
def handle_request(entry_name=None):
    """Main endpoint. Dispatches to LQN engine or legacy handler."""
    config = load_task_config()

    if config:
        return handle_lqn_request(entry_name, config)
    return handle_legacy_request()


def handle_lqn_request(entry_name: str | None, config: dict):
    """Handle request using LQN task configuration."""
    my_name = os.environ.get("SERVICE_NAME", "generic-service")
    entries = config.get("entries", {})
    dry_run = _is_dry_run()
    trace_enabled = dry_run or os.environ.get("LQN_TRACE", "0") == "1"
    trace = [] if trace_enabled else None

    if not entry_name:
        entry_name = next(iter(entries)) if entries else None

    if not entry_name or entry_name not in entries:
        return jsonify({"error": f"Unknown entry: {entry_name}"}), 404

    results = execute_activity_graph(entry_name, config, trace, dry_run)

    response = {
        "message": f"Response from {my_name}",
        "entry": entry_name,
        "outbound_results": results,
    }
    if trace is not None:
        response["trace"] = trace
    return jsonify(response)


def handle_legacy_request():
    """Legacy handler using SERVICE_TIME_SECONDS + OUTBOUND_CALLS env vars."""
    my_name = os.environ.get("SERVICE_NAME", "generic-service")

    # 1. Simulate the service's own workload first
    do_work()

    # 2. Parse the downstream call configuration
    probabilistic_targets, fixed_targets = parse_outbound_calls()
    results = []

    # 3. Execute all fixed calls (probability >= 1.0)
    for target in fixed_targets:
        if target["type"] == "SYNC":
            results.append(make_call(target))
        elif target["type"] == "ASYNC":
            make_async_call_pooled(target)
            results.append({"service": target["service"], "status": "async_pooled"})

    # 4. Choose and execute one of the probabilistic calls
    if probabilistic_targets:
        services = [t["service"] for t in probabilistic_targets]
        weights = [t["probability"] for t in probabilistic_targets]
        chosen_service_name = random.choices(services, weights=weights, k=1)[0]
        chosen_target = next(
            t for t in probabilistic_targets if t["service"] == chosen_service_name
        )

        if chosen_target["type"] == "SYNC":
            results.append(make_call(chosen_target))
        elif chosen_target["type"] == "ASYNC":
            make_async_call_pooled(chosen_target)
            results.append(
                {"service": chosen_target["service"], "status": "async_pooled"}
            )

    return jsonify({"message": f"Response from {my_name}", "outbound_results": results})
