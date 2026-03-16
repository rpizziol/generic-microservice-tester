"""Unit tests for locustfile generator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from locustfile_gen import generate_locustfile
from lqn_parser import parse_lqn_file

GROUNDTRUTH = Path(__file__).parent.parent.parent / "test" / "lqn-groundtruth"


class TestSingleEntryModel:
    """validation-model.lqn: TClient calls process×1.0 sync."""

    @pytest.fixture()
    def source(self) -> str:
        model = parse_lqn_file(str(GROUNDTRUTH / "validation-model.lqn"))
        return generate_locustfile(model)

    def test_generates_valid_python(self, source: str) -> None:
        compile(source, "<test>", "exec")

    def test_contains_locust_imports(self, source: str) -> None:
        assert "from locust import HttpUser, task" in source

    def test_think_time(self, source: str) -> None:
        assert "THINK_TIME = 2.0" in source

    def test_host_resolved(self, source: str) -> None:
        assert 'host = "http://tserver-svc"' in source

    def test_single_sync_call(self, source: str) -> None:
        assert 'self.client.get("http://tserver-svc/process")' in source

    def test_no_random_for_integer_calls(self, source: str) -> None:
        assert "random.random()" not in source


class TestMultiEntryModel:
    """template_annotated.lqn: TClient calls visit×3, save×1, read×1, buy×1.2, notify×1 async."""

    @pytest.fixture()
    def source(self) -> str:
        model = parse_lqn_file(str(GROUNDTRUTH / "template_annotated.lqn"))
        return generate_locustfile(model)

    def test_generates_valid_python(self, source: str) -> None:
        compile(source, "<test>", "exec")

    def test_multiple_calls(self, source: str) -> None:
        assert "tserver-svc/visit" in source
        assert "tserver-svc/save" in source
        assert "tserver-svc/buy" in source
        assert "tfileserver-svc/read" in source

    def test_visit_called_3_times(self, source: str) -> None:
        assert "for _ in range(3):" in source
        assert "tserver-svc/visit" in source

    def test_fractional_calls_buy(self, source: str) -> None:
        # buy×1.2: 1 guaranteed + 0.2 probability
        assert "random.random() < 0.2" in source
        assert "tserver-svc/buy" in source

    def test_async_calls_included(self, source: str) -> None:
        assert "tserver-svc/notify" in source
        assert "async" in source.lower()

    def test_exponential_wait_time(self, source: str) -> None:
        assert "expovariate" in source

    def test_think_time_from_model(self, source: str) -> None:
        assert "THINK_TIME = 0.01" in source

    def test_cross_service_url(self, source: str) -> None:
        """read entry is on TFileServer, not TServer."""
        assert "http://tfileserver-svc/read" in source


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_no_reference_task_raises(self) -> None:
        model = parse_lqn_file(str(GROUNDTRUTH / "validation-model.lqn"))
        # Remove reference tasks
        model.tasks = [t for t in model.tasks if not t.is_reference]
        with pytest.raises(ValueError, match="No reference task"):
            generate_locustfile(model)
