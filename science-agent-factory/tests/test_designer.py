"""Data Designer 批量生成的调度与隔离回归测试。

测试目的：验证真实 Data Designer 调度、原生任务生成边界和失败隔离。
被测接口：designer.run_batch、generate_task。
输入与夹具：临时 JSONL/配置/SeedBundle、替代科学生成后端和线程屏障。
预期结果：两行确实并行，输出隔离，失败有记录，不调用 Distiller、Solver 或覆盖旧批次。
"""

import asyncio
import json
import threading

import pytest

pytest.importorskip("data_designer")
from science_agent_factory import designer


def requests(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("allow_host_full_access: true\n")
    rows = []
    for identifier in ("one", "two", "bad"):
        seed = tmp_path / identifier
        seed.mkdir()
        (seed / "seed_bundle.json").write_text("{}")
        rows.append({"request_id": identifier, "config": "config.yaml", "seed": identifier})
    path = tmp_path / "requests.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_real_designer_parallel_generation_and_failure_isolation(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    observed = []

    class Factory:
        def __init__(self, settings):
            self.settings = settings

        def job_dir(self, job_id):
            return self.settings.storage_root / "jobs" / job_id

        async def generate_tasks(self, seed, *, job_id):
            observed.append(str(self.settings.storage_root))
            if seed.name == "bad":
                raise RuntimeError("native synthesis failed")
            await asyncio.to_thread(barrier.wait, 30)
            return {
                "candidates": [{"stage": "preflight_accepted", "task_dir": "task", "attempts": []}]
            }

    monkeypatch.setattr(designer, "ScienceAgentFactory", Factory)
    path = requests(tmp_path)
    output = tmp_path / "batch"
    results = designer.run_batch(path, output, concurrency=2)
    assert {r["request_id"]: r["status"] for r in results} == {
        "one": "completed",
        "two": "completed",
        "bad": "failed",
    }
    assert len(set(observed)) == 3
    assert "native synthesis failed" in results[0]["error"]
    assert all((output / "items" / r["request_id"] / "result.json").is_file() for r in results)
    assert (output / "results.json").is_file()
    with pytest.raises(FileExistsError):
        designer.run_batch(path, output)
    assert len(observed) == 3


@pytest.mark.parametrize("identifier", ["../escape", "duplicate"])
def test_rejects_unsafe_and_duplicate_identifiers(tmp_path, identifier):
    path = requests(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[1]["request_id"] = "one" if identifier == "duplicate" else identifier
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError):
        designer.run_batch(path, tmp_path / "batch")
    assert not (tmp_path / "batch").exists()


def test_batch_runs_native_audited_factory_without_solver(tmp_path, monkeypatch):
    from tests.integration.test_harbor_workflow import FakeRunner, FakeRuntime
    from tests.unit.test_harbor_task import _seed_bundle

    seed, _ = _seed_bundle(tmp_path)
    harbor = tmp_path / "harbor.yaml"
    harbor.write_text("agents: [{name: codex, model_name: test-model}]\n")
    config = tmp_path / "config.yaml"
    config.write_text(
        "allow_host_full_access: true\n"
        "pipeline:\n  workflow_mode: audited\n  task_count: 1\n"
        "harbor:\n  config_file: harbor.yaml\n"
    )
    path = tmp_path / "requests.jsonl"
    path.write_text(json.dumps({"request_id": "native", "seed": str(seed), "config": str(config)}))
    runtime, runner = FakeRuntime(), FakeRunner()
    factory_type = designer.ScienceAgentFactory

    def factory(settings):
        return factory_type(settings, runtime=runtime, runner=runner)

    monkeypatch.setattr(designer, "ScienceAgentFactory", factory)
    results = designer.run_batch(path, tmp_path / "batch", concurrency=1)
    assert results[0]["status"] == "completed", results
    assert not runner.calls
    assert {str(call[0]) for call in runtime.calls} == {"builder", "auditor"}
    for task in results[0]["task_paths"]:
        from pathlib import Path

        assert (Path(task) / "task.toml").is_file()
    assert all(not c["attempts"] for c in results[0]["native_state"]["candidates"])
