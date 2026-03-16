"""Unit tests for deploy script generator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from deploy_gen import generate_deploy_script

GROUNDTRUTH = Path(__file__).parent.parent.parent / "test" / "lqn-groundtruth"
MODEL = str(GROUNDTRUTH / "validation-model.lqn")


@pytest.fixture()
def script() -> str:
    return generate_deploy_script(MODEL)


class TestBashValidity:
    """Generated script should be valid bash."""

    def test_shebang(self, script: str) -> None:
        assert script.startswith("#!/usr/bin/env bash")

    def test_set_euo_pipefail(self, script: str) -> None:
        assert "set -euo pipefail" in script

    def test_valid_bash_syntax(self, script: str, tmp_path: Path) -> None:
        """bash -n checks syntax without executing."""
        f = tmp_path / "deploy.sh"
        f.write_text(script)
        import subprocess

        result = subprocess.run(
            ["bash", "-n", str(f)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"Bash syntax error:\n{result.stderr}"


class TestScriptContent:
    """Verify key content in the generated script."""

    def test_namespace_from_model(self, script: str) -> None:
        assert 'NAMESPACE="gmt-validation-model"' in script

    def test_custom_namespace(self) -> None:
        s = generate_deploy_script(MODEL, namespace="my-ns")
        assert 'NAMESPACE="my-ns"' in s

    def test_contains_k8s_manifests(self, script: str) -> None:
        assert "kind: Deployment" in script
        assert "kind: Service" in script
        assert "tserver-deployment" in script

    def test_contains_up_down_test_commands(self, script: str) -> None:
        assert "cmd_up()" in script
        assert "cmd_down()" in script
        assert "cmd_test()" in script

    def test_locust_job_manifest(self, script: str) -> None:
        assert "locustio/locust" in script
        assert "locust-loadtest" in script
        assert "--headless" in script

    def test_configmap_for_locustfile(self, script: str) -> None:
        assert "gmt-locustfile" in script
        assert "ConfigMap" in script.lower() or "configmap" in script

    def test_users_from_multiplicity(self, script: str) -> None:
        # validation-model has m=1 for TClient
        assert "${1:-1}" in script

    def test_custom_image(self) -> None:
        s = generate_deploy_script(MODEL, image="myregistry/gmt:v2")
        assert "myregistry/gmt:v2" in s
