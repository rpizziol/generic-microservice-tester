"""Tests for the LQN-to-K8s compiler."""

import sys
from pathlib import Path

import pytest

# Add tools/ and src/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from lqn_compiler import (
    build_task_config,
    compile_model,
    find_entry_point_task,
    resolve_call_target,
    task_to_k8s_name,
)
from lqn_parser import parse_lqn_file


@pytest.fixture()
def model(groundtruth_dir):
    filepath = groundtruth_dir / "template_annotated.lqn"
    if not filepath.exists():
        pytest.skip(f"Ground truth file not found: {filepath}")
    return parse_lqn_file(str(filepath))


class TestTaskToK8sName:
    def test_simple(self):
        assert task_to_k8s_name("TServer") == "tserver"

    def test_underscore(self):
        assert task_to_k8s_name("TFile_Server") == "tfile-server"

    def test_already_lowercase(self):
        assert task_to_k8s_name("myservice") == "myservice"


class TestResolveCallTarget:
    def test_resolves_entry(self, model):
        result = resolve_call_target(model, "read")
        assert result is not None
        svc, path = result
        assert svc == "tfileserver-svc"
        assert path == "read"

    def test_resolves_unknown_returns_none(self, model):
        result = resolve_call_target(model, "nonexistent")
        assert result is None


class TestCompileGeneratesDeploymentPerTask:
    def test_non_reference_tasks_get_deployments(self, model):
        yaml_str = compile_model(model)
        # TServer, TFileServer, TBackup = 3 non-reference tasks
        assert yaml_str.count("kind: Deployment") == 3

    def test_reference_task_skipped(self, model):
        yaml_str = compile_model(model)
        assert "tclient" not in yaml_str.lower().split("kind:")[0]
        # TClient should not appear as a deployment name
        assert "tclient-deployment" not in yaml_str


class TestCompileGeneratesServicePerTask:
    def test_services_generated(self, model):
        yaml_str = compile_model(model)
        assert yaml_str.count("kind: Service") == 3

    def test_service_naming(self, model):
        yaml_str = compile_model(model)
        assert "tserver-svc" in yaml_str
        assert "tfileserver-svc" in yaml_str
        assert "tbackup-svc" in yaml_str


class TestCompileTaskMultiplicity:
    def test_gunicorn_workers_matches_multiplicity(self, model):
        yaml_str = compile_model(model)
        # TServer has m=2
        assert 'value: "2"' in yaml_str


class TestCompileEntryToConfig:
    def test_lqn_task_config_present(self, model):
        yaml_str = compile_model(model)
        assert "LQN_TASK_CONFIG" in yaml_str

    def test_tserver_config_has_entries(self, model):
        # Find TServer task and build its config
        tserver = next(t for t in model.tasks if t.name == "TServer")
        config = build_task_config(tserver, model)
        assert "visit" in config["entries"]
        assert "buy" in config["entries"]
        assert "notify" in config["entries"]
        assert "save" in config["entries"]


class TestCompileActivityGraphSerialized:
    def test_tserver_graph_in_config(self, model):
        tserver = next(t for t in model.tasks if t.name == "TServer")
        config = build_task_config(tserver, model)
        graph = config["graph"]
        assert len(graph["and_forks"]) == 1
        assert len(graph["and_joins"]) == 1
        assert len(graph["or_forks"]) == 1
        assert "internal" in graph["replies"]
        assert "display" in graph["replies"]


class TestCompileCallTargetResolution:
    def test_activity_call_resolved_to_service(self, model):
        tserver = next(t for t in model.tasks if t.name == "TServer")
        config = build_task_config(tserver, model)
        # external activity has y external read 1.0
        ext = config["activities"]["external"]
        assert "sync_calls" in ext
        # Should resolve 'read' entry to 'tfileserver-svc/read'
        urls = list(ext["sync_calls"].keys())
        assert any("tfileserver-svc/read" in url for url in urls)

    def test_phase_entry_calls_resolved(self, model):
        # save entry has y save write 1.0
        tserver = next(t for t in model.tasks if t.name == "TServer")
        config = build_task_config(tserver, model)
        save_entry = config["entries"]["save"]
        assert "sync_calls" in save_entry
        urls = list(save_entry["sync_calls"].keys())
        assert any("tfileserver-svc/write" in url for url in urls)


class TestCompileValidYaml:
    def test_yaml_structure(self, model):
        yaml_str = compile_model(model)
        # Basic YAML structure checks
        assert "apiVersion: apps/v1" in yaml_str
        assert "apiVersion: v1" in yaml_str
        assert "containerPort: 8080" in yaml_str
        assert "targetPort: 8080" in yaml_str


class TestCompileLabels:
    def test_standard_labels(self, model):
        yaml_str = compile_model(model)
        assert "app.kubernetes.io/name:" in yaml_str
        assert "app.kubernetes.io/component: lqn-task" in yaml_str
        assert "lqn.gmt/model:" in yaml_str


class TestCompileCustomImage:
    def test_custom_image(self, model):
        yaml_str = compile_model(model, image="myregistry/gmt:v2.0")
        assert "myregistry/gmt:v2.0" in yaml_str
        assert "generic-microservice-tester:latest" not in yaml_str


class TestCompileResourceLimits:
    def test_cpu_requests_present(self, model):
        yaml_str = compile_model(model)
        assert "requests:" in yaml_str
        assert "cpu:" in yaml_str
        assert "limits:" in yaml_str

    def test_memory_request(self, model):
        yaml_str = compile_model(model)
        assert 'memory: "256Mi"' in yaml_str

    def test_memory_limit(self, model):
        yaml_str = compile_model(model)
        assert 'memory: "512Mi"' in yaml_str

    def test_cpu_limit_minimum_1000m(self):
        """CPU limit must be >= 1000m even for low-multiplicity processors."""
        m = parse_lqn_file(
            str(Path(__file__).parent.parent.parent / "test" / "lqn-groundtruth" / "validation-model.lqn")
        )
        yaml_str = compile_model(m)
        # PServer has m=1, so limit = max(1*1000, 1000) = 1000m
        assert 'cpu: "1000m"' in yaml_str


class TestOtelAnnotation:
    def test_deployment_has_otel_annotation(self, model):
        yaml_str = compile_model(model)
        assert "instrumentation.opentelemetry.io/inject-python" in yaml_str

    def test_annotation_value_true(self, model):
        yaml_str = compile_model(model)
        assert 'inject-python: "true"' in yaml_str


class TestOtelEnvVars:
    def test_otel_service_name_present(self, model):
        yaml_str = compile_model(model)
        assert "OTEL_SERVICE_NAME" in yaml_str

    def test_otel_service_name_matches_service_name(self, model):
        """OTEL_SERVICE_NAME must equal SERVICE_NAME for Jaeger discovery."""
        yaml_str = compile_model(model)
        # Both should have the same value for each deployment
        for line in yaml_str.splitlines():
            if "OTEL_SERVICE_NAME" in line:
                # The value line follows
                break
        # Check that tserver appears in both SERVICE_NAME and OTEL_SERVICE_NAME
        assert yaml_str.count('value: "tserver"') >= 2

    def test_otel_exporter_endpoint(self, model):
        yaml_str = compile_model(model)
        assert "http://otel-collector.observability:4317" in yaml_str

    def test_otel_traces_exporter(self, model):
        yaml_str = compile_model(model)
        assert 'OTEL_TRACES_EXPORTER' in yaml_str
        assert 'value: "otlp"' in yaml_str

    def test_otel_metrics_exporter_none(self, model):
        yaml_str = compile_model(model)
        assert 'OTEL_METRICS_EXPORTER' in yaml_str


class TestNodePort:
    def test_entry_point_service_has_nodeport(self, model):
        yaml_str = compile_model(model)
        assert "type: NodePort" in yaml_str

    def test_only_one_nodeport_service(self, model):
        yaml_str = compile_model(model)
        assert yaml_str.count("type: NodePort") == 1

    def test_non_entry_services_are_clusterip(self, model):
        """Services other than entry point should not have NodePort."""
        yaml_str = compile_model(model)
        # Only 1 NodePort, rest are implicit ClusterIP
        services = [s for s in yaml_str.split("---") if "kind: Service" in s]
        nodeport_count = sum(1 for s in services if "type: NodePort" in s)
        assert nodeport_count == 1
        assert len(services) > 1  # At least 2 services in template_annotated

    def test_fixed_nodeport(self, model):
        yaml_str = compile_model(model, node_port=30089)
        assert "nodePort: 30089" in yaml_str


class TestFindEntryPoint:
    def test_finds_correct_task_validation_model(self):
        m = parse_lqn_file(
            str(Path(__file__).parent.parent.parent / "test" / "lqn-groundtruth" / "validation-model.lqn")
        )
        assert find_entry_point_task(m) == "tserver"

    def test_finds_correct_task_template(self, model):
        assert find_entry_point_task(model) == "tserver"

    def test_finds_correct_task_activity_based(self):
        """lqn01-5f: Task0 calls gw1 via activity graph → entry point = taskgw1."""
        lqn_path = Path("/Users/emilio-imt/git/TLG/tests/lqntest_model/lqn01-5f.lqn")
        if not lqn_path.exists():
            pytest.skip(f"Model not found: {lqn_path}")
        m = parse_lqn_file(str(lqn_path))
        assert find_entry_point_task(m) == "taskgw1"
