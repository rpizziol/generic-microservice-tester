#!/usr/bin/env python3
"""LQN-to-K8s Compiler: generates Kubernetes manifests from LQN models.

Reads a .lqn file, parses it, and generates Deployment + Service YAML
for each non-reference task. Reference tasks (load generators) are skipped.

Usage:
    python tools/lqn_compiler.py model.lqn
    python tools/lqn_compiler.py model.lqn | kubectl apply -f -
    python tools/lqn_compiler.py --image myregistry/gmt:v1 model.lqn
    python tools/lqn_compiler.py model.lqn -o output.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from gmt.lqn_parser import LqnModel, LqnTask, parse_lqn_file
except ImportError:
    # Fallback for running directly without pip install
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from lqn_parser import LqnModel, LqnTask, parse_lqn_file


def task_to_k8s_name(task_name: str) -> str:
    """Convert LQN task name to K8s-safe lowercase name."""
    return task_name.lower().replace("_", "-")


def resolve_call_target(model: LqnModel, entry_name: str) -> tuple[str, str] | None:
    """Resolve an LQN entry name to (k8s_service_url, entry_path).

    Returns the K8s service DNS name + entry path for HTTP routing.
    E.g., entry 'read' on task 'TFileServer' → ('tfileserver-svc', 'read')
    """
    for task in model.tasks:
        for entry in task.entries:
            if entry.name == entry_name:
                svc_name = f"{task_to_k8s_name(task.name)}-svc"
                return svc_name, entry.name
    return None


def build_task_config(task: LqnTask, model: LqnModel) -> dict:
    """Build the LQN_TASK_CONFIG JSON for a task."""
    config: dict = {
        "task_name": task.name,
        "entries": {},
        "activities": {},
        "graph": {
            "sequences": [],
            "or_forks": [],
            "and_forks": [],
            "and_joins": [],
            "replies": {},
        },
    }

    # Build entries
    for entry in task.entries:
        entry_config: dict = {}

        if entry.start_activity:
            entry_config["start_activity"] = entry.start_activity
        else:
            # Phase-based entry
            if entry.phase_service_times:
                entry_config["service_time"] = entry.phase_service_times[0]

            # Sync calls (Phase 1 only for now)
            if entry.phase_sync_calls:
                sync_calls = {}
                for target_entry, phases in entry.phase_sync_calls.items():
                    mean_calls = phases[0] if phases else 0.0
                    if mean_calls > 0:
                        resolved = resolve_call_target(model, target_entry)
                        if resolved:
                            svc, path = resolved
                            url = f"{svc}/{path}"
                            sync_calls[url] = mean_calls
                if sync_calls:
                    entry_config["sync_calls"] = sync_calls

            # Async calls (Phase 1 only)
            if entry.phase_async_calls:
                async_calls = {}
                for target_entry, phases in entry.phase_async_calls.items():
                    mean_calls = phases[0] if phases else 0.0
                    if mean_calls > 0:
                        resolved = resolve_call_target(model, target_entry)
                        if resolved:
                            svc, path = resolved
                            url = f"{svc}/{path}"
                            async_calls[url] = mean_calls
                if async_calls:
                    entry_config["async_calls"] = async_calls

        config["entries"][entry.name] = entry_config

    # Build activities
    for act_name, act in task.activities.items():
        act_config: dict = {}
        if act.service_time > 0:
            act_config["service_time"] = act.service_time

        # Resolve sync call targets
        if act.sync_calls:
            sync_calls = {}
            for target_entry, mean_calls in act.sync_calls:
                resolved = resolve_call_target(model, target_entry)
                if resolved:
                    svc, path = resolved
                    url = f"{svc}/{path}"
                    sync_calls[url] = mean_calls
            if sync_calls:
                act_config["sync_calls"] = sync_calls

        # Resolve async call targets
        if act.async_calls:
            async_calls = {}
            for target_entry, mean_calls in act.async_calls:
                resolved = resolve_call_target(model, target_entry)
                if resolved:
                    svc, path = resolved
                    url = f"{svc}/{path}"
                    async_calls[url] = mean_calls
            if async_calls:
                act_config["async_calls"] = async_calls

        config["activities"][act_name] = act_config

    # Build activity graph
    if task.activity_graph:
        graph = task.activity_graph
        config["graph"]["sequences"] = list(graph.sequences)
        config["graph"]["and_forks"] = [
            {"from": src, "branches": branches} for src, branches in graph.and_forks
        ]
        config["graph"]["and_joins"] = [
            {"branches": branches, "to": target} for branches, target in graph.and_joins
        ]
        config["graph"]["or_forks"] = [
            {
                "from": src,
                "branches": [{"prob": p, "to": n} for p, n in branches],
            }
            for src, branches in graph.or_forks
        ]
        config["graph"]["replies"] = dict(graph.replies)

    return config


def get_processor_multiplicity(model: LqnModel, proc_name: str) -> int | None:
    """Get processor multiplicity for CPU resource limits."""
    for proc in model.processors:
        if proc.name == proc_name:
            return proc.multiplicity
    return None


def generate_deployment_yaml(
    task: LqnTask,
    model: LqnModel,
    image: str,
    namespace: str | None,
) -> str:
    """Generate K8s Deployment YAML for a task."""
    k8s_name = task_to_k8s_name(task.name)
    config = build_task_config(task, model)
    config_json = json.dumps(config, separators=(",", ":"))

    proc_mult = get_processor_multiplicity(model, task.processor)
    cpu_request = f"{proc_mult * 100}m" if proc_mult and proc_mult < 100 else "500m"

    ns_line = f"\n  namespace: {namespace}" if namespace else ""

    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {k8s_name}-deployment{ns_line}
  labels:
    app.kubernetes.io/name: {k8s_name}
    app.kubernetes.io/component: lqn-task
    lqn.gmt/model: {model.name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {k8s_name}
  template:
    metadata:
      labels:
        app: {k8s_name}
    spec:
      containers:
      - name: app
        image: {image}
        ports:
        - containerPort: 8080
        resources:
          requests:
            cpu: "{cpu_request}"
          limits:
            cpu: "{cpu_request}"
        env:
        - name: SERVICE_NAME
          value: "{k8s_name}"
        - name: GUNICORN_WORKERS
          value: "{task.multiplicity}"
        - name: LQN_TASK_CONFIG
          value: '{config_json}'"""


def generate_service_yaml(
    task: LqnTask,
    namespace: str | None,
) -> str:
    """Generate K8s Service YAML for a task."""
    k8s_name = task_to_k8s_name(task.name)
    ns_line = f"\n  namespace: {namespace}" if namespace else ""

    return f"""apiVersion: v1
kind: Service
metadata:
  name: {k8s_name}-svc{ns_line}
spec:
  selector:
    app: {k8s_name}
  ports:
  - port: 80
    targetPort: 8080"""


def compile_model(
    model: LqnModel,
    image: str = "generic-microservice-tester:latest",
    namespace: str | None = None,
) -> str:
    """Compile an LQN model to K8s YAML manifests."""
    manifests = []

    for task in model.tasks:
        if task.is_reference:
            continue

        deployment = generate_deployment_yaml(task, model, image, namespace)
        service = generate_service_yaml(task, namespace)
        manifests.append(deployment)
        manifests.append(service)

    return "\n---\n".join(manifests)


def main():
    parser = argparse.ArgumentParser(
        description="Compile LQN model to Kubernetes manifests"
    )
    parser.add_argument("lqn_file", help="Path to .lqn model file")
    parser.add_argument(
        "--image",
        default="generic-microservice-tester:latest",
        help="Docker image for GMT containers",
    )
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument("--namespace", help="K8s namespace for resources")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be generated"
    )

    args = parser.parse_args()

    model = parse_lqn_file(args.lqn_file)
    yaml_output = compile_model(model, image=args.image, namespace=args.namespace)

    if args.dry_run:
        print(f"# Would generate manifests for: {model.name}")
        non_ref = [t for t in model.tasks if not t.is_reference]
        for t in non_ref:
            print(f"#   Deployment + Service: {task_to_k8s_name(t.name)}")
        print(f"# Total: {len(non_ref)} deployments, {len(non_ref)} services")
        print("---")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(yaml_output + "\n")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(yaml_output)


if __name__ == "__main__":
    main()
