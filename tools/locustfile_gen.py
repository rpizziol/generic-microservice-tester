#!/usr/bin/env python3
"""Generate a Locust locustfile from the reference task of an LQN model.

The reference task (load generator) defines the closed-loop workload:
think time (z), number of clients (m), and which entries to call (y/z).

Usage:
    python tools/locustfile_gen.py model.lqn
    python tools/locustfile_gen.py model.lqn -o locustfile.py
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    from gmt.lqn_parser import LqnModel, LqnTask, parse_lqn_file
    from gmt.tools.lqn_compiler import resolve_call_target
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).parent))
    from lqn_parser import LqnModel, LqnTask, parse_lqn_file
    from lqn_compiler import resolve_call_target


def _extract_activity_calls(
    task: LqnTask,
) -> list[tuple[str, float, str]]:
    """Walk the activity graph of a task and extract all sync/async calls.

    Returns list of (target_entry, mean_calls, call_type) from all activities
    reachable via DFS from each entry's start_activity.
    """
    calls: list[tuple[str, float, str]] = []
    visited: set[str] = set()

    def dfs(activity_name: str) -> None:
        if activity_name in visited or activity_name not in task.activities:
            return
        visited.add(activity_name)

        act = task.activities[activity_name]
        for target_entry, mean_calls in act.sync_calls:
            calls.append((target_entry, mean_calls, "sync"))
        for target_entry, mean_calls in act.async_calls:
            calls.append((target_entry, mean_calls, "async"))

        # Follow graph edges
        graph = task.activity_graph
        if not graph:
            return
        for src, dst in graph.sequences:
            if src == activity_name:
                dfs(dst)
        for src, branches in graph.and_forks:
            if src == activity_name:
                for b in branches:
                    dfs(b)
        for branches, dst in graph.and_joins:
            if activity_name in branches:
                dfs(dst)
        for src, branch_list in graph.or_forks:
            if src == activity_name:
                for _, name in branch_list:
                    dfs(name)

    for entry in task.entries:
        if entry.start_activity:
            dfs(entry.start_activity)

    return calls


def _resolve_url(model: LqnModel, entry_name: str) -> str | None:
    """Resolve an LQN entry name to a K8s HTTP URL (port 80)."""
    result = resolve_call_target(model, entry_name)
    if result is None:
        return None
    svc_name, path = result
    return f"http://{svc_name}/{path}"


def _build_call_list(
    model: LqnModel,
    sync_calls: dict[str, list[float]] | None,
    async_calls: dict[str, list[float]] | None,
) -> list[tuple[str, float, str]]:
    """Build ordered list of (url, mean_calls, call_type) from entry calls.

    Returns calls in definition order: sync first, then async.
    """
    calls: list[tuple[str, float, str]] = []

    for call_dict, call_type in [
        (sync_calls, "sync"),
        (async_calls, "async"),
    ]:
        if not call_dict:
            continue
        for target_entry, phases in call_dict.items():
            mean_calls = phases[0] if phases else 0.0
            if mean_calls <= 0:
                continue
            url = _resolve_url(model, target_entry)
            if url is None:
                continue
            calls.append((url, mean_calls, call_type))

    return calls


def _generate_call_block(calls: list[tuple[str, float, str]], indent: str = "        ") -> str:
    """Generate Python code for executing all calls in a cycle."""
    if not calls:
        return f"{indent}pass  # reference task has no outbound calls"

    lines: list[str] = []
    for url, mean_calls, call_type in calls:
        n_guaranteed = math.floor(mean_calls)
        frac = round(mean_calls - n_guaranteed, 6)

        comment = f"# {mean_calls} {call_type} call(s) to {url.split('/')[-1]}"
        lines.append(f"{indent}{comment}")

        if n_guaranteed == 1 and frac == 0:
            lines.append(f'{indent}self.client.get("{url}")')
        elif n_guaranteed > 0:
            lines.append(f"{indent}for _ in range({n_guaranteed}):")
            lines.append(f'{indent}    self.client.get("{url}")')

        if frac > 0:
            lines.append(f"{indent}if random.random() < {frac}:")
            lines.append(f'{indent}    self.client.get("{url}")')

    return "\n".join(lines)


def generate_locustfile(model: LqnModel) -> str:
    """Generate a Locust locustfile from the reference task of an LQN model.

    The locustfile implements one LqnClient(HttpUser) with a single @task
    method that executes all calls per activation cycle, matching LQN semantics.

    Raises:
        ValueError: If no reference task found in the model.
    """
    ref_tasks = [t for t in model.tasks if t.is_reference]
    if not ref_tasks:
        raise ValueError(f"No reference task found in model '{model.name}'")

    ref = ref_tasks[0]
    think_time = ref.think_time
    multiplicity = ref.multiplicity

    # Collect all calls from the reference task.
    # Phase-based entries have calls in phase_sync_calls/phase_async_calls.
    # Activity-based entries have calls in the activity objects — walk the graph.
    all_calls: list[tuple[str, float, str]] = []
    for entry in ref.entries:
        phase_calls = _build_call_list(
            model, entry.phase_sync_calls, entry.phase_async_calls
        )
        if phase_calls:
            all_calls.extend(phase_calls)

    if not all_calls:
        # Try activity-based extraction (DFS over activity graph)
        activity_calls = _extract_activity_calls(ref)
        for target_entry, mean_calls, call_type in activity_calls:
            url = _resolve_url(model, target_entry)
            if url and mean_calls > 0:
                all_calls.append((url, mean_calls, call_type))

    call_block = _generate_call_block(all_calls)

    # Determine first target service for host default
    first_svc = "http://localhost"
    if all_calls:
        # Extract scheme + host from first URL
        first_url = all_calls[0][0]
        parts = first_url.split("/")
        first_svc = f"{parts[0]}//{parts[2]}"

    needs_random = "random.random()" in call_block

    source = f'''"""Auto-generated locustfile from LQN model: {model.name}

Reference task: {ref.name} (m={multiplicity}, z={think_time})
Generated by: lqn-locustfile (GMT)
"""
{"import random" if needs_random else ""}
from locust import HttpUser, task

THINK_TIME = {think_time}


class LqnClient(HttpUser):
    """Closed-loop client: {multiplicity} users, think time z={think_time}s (exponential)."""

    host = "{first_svc}"

    def wait_time(self):
        """Exponential think time matching LQN model semantics."""
        {"return random.expovariate(1.0 / THINK_TIME) if THINK_TIME > 0 else 0" if needs_random else "import random; return random.expovariate(1.0 / THINK_TIME) if THINK_TIME > 0 else 0"}

    @task
    def cycle(self):
        """One activation of the reference task: execute all calls in sequence."""
{call_block}
'''
    return source.strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Locust locustfile from LQN model reference task"
    )
    parser.add_argument("lqn_file", help="Path to .lqn model file")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")

    args = parser.parse_args()

    model = parse_lqn_file(args.lqn_file)

    try:
        source = generate_locustfile(model)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        Path(args.output).write_text(source)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(source)


if __name__ == "__main__":
    main()
