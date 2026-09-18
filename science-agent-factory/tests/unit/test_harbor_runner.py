"""与 harness 无关的 Harbor 进程协议测试。

测试目的：确保原生配置透传、结果归档、进程失败和取消可辨别。
被测接口：HarborRunner、freeze_config、load_config。
输入与夹具：临时可执行程序模拟 Harbor CLI，不调用网络或 Docker。
预期结果：任意 agent 配置保持不变，失败轨迹留存，子进程可靠结束。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from science_agent_factory.agents.harbor import HarborRunner, freeze_config, load_config
from science_agent_factory.config import FactorySettings
from tests.unit.test_harbor_task import _harbor_task


@pytest.fixture
def runner_setup(tmp_path):
    executable = tmp_path / "fake-harbor"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys, time
from pathlib import Path
import yaml
config = yaml.safe_load(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
root = Path(config['jobs_dir']) / config['job_name'] / 'trial'
(root / 'agent').mkdir(parents=True)
(root / 'agent/trajectory.json').write_text(json.dumps({'schema_version': 'ATIF-v1.6', 'steps': [
    {'step_id': 1, 'source': 'user', 'message': 'real user'},
    {'step_id': 2, 'source': 'agent', 'message': 'finished', 'reasoning_content': 'visible reasoning'}]}))
mode = config.get('metadata', {}).get('test_mode', 'pass')
result = {'trial_name': 'trial', 'finished_at': '2026-09-17T00:00:00Z', 'agent_info': {'name': 'custom', 'model_info': {'name': 'local/model'}}}
if mode != 'unverified':
    result['verifier_result'] = {'rewards': {'reward': 0 if mode == 'fail' else 1}}
if mode == 'malformed':
    result['agent_info'] = []
if mode == 'malformed-name':
    result['agent_info']['name'] = ['invalid']
if mode == 'wrong-trial':
    result['trial_name'] = 'another-trial'
(root / 'result.json').write_text(json.dumps(result))
(root.parent / 'cwd.txt').write_text(os.getcwd())
if mode == 'sleep':
    time.sleep(60)
sys.exit(2 if mode == 'error' else 0)
"""
    )
    executable.chmod(0o755)
    native = tmp_path / "native.yaml"
    native.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "import_path": "my.module:Agent",
                        "model_name": "anything",
                        "env": {"TOKEN": "${EXTERNAL_TOKEN}"},
                        "kwargs": {"custom_option": 9},
                    }
                ],
                "environment": {"type": "docker"},
            }
        )
    )
    settings = FactorySettings.load_default()
    settings.pipeline.workflow_mode = type(settings.pipeline.workflow_mode).LIGHTWEIGHT
    settings.harbor.executable = str(executable)
    settings.harbor.config_file = native
    settings.harbor.shutdown_grace_s = 1
    runner = HarborRunner(settings, config_file=native, config_base_dir=tmp_path)
    return runner, _harbor_task(tmp_path), native


def set_mode(native, mode):
    config = load_config(native)
    config["metadata"] = {"test_mode": mode}
    native.write_text(yaml.safe_dump(config))


@pytest.mark.parametrize(
    "mode,outcome,passed,verified",
    [
        ("pass", "completed", True, True),
        ("fail", "completed", False, True),
        ("unverified", "completed", False, False),
        ("error", "error", False, True),
        ("malformed", "error", False, False),
        ("malformed-name", "error", False, False),
        ("wrong-trial", "error", False, False),
    ],
)
async def test_native_config_and_artifacts(runner_setup, tmp_path, mode, outcome, passed, verified):
    runner, task, native = runner_setup
    set_mode(native, mode)
    result = await runner.run(task_dir=task, attempt_dir=tmp_path / "attempt", run_id="run")
    assert result.outcome == outcome
    assert result.passed == passed
    assert result.verification_executed == verified
    assert result.trajectory_path == "trajectory.json"
    config = yaml.safe_load((tmp_path / "attempt/harbor-config.yaml").read_text())
    assert config["agents"] == load_config(native)["agents"]
    assert config["n_attempts"] == config["n_concurrent_trials"] == 1
    assert config["retry"]["max_retries"] == 0
    assert (tmp_path / "attempt/harbor/run/cwd.txt").read_text() == str(tmp_path)
    assert json.loads((tmp_path / "attempt/result.json").read_text())["outcome"] == outcome


async def test_timeout_keeps_valid_failure_trajectory(runner_setup, tmp_path):
    runner, task, native = runner_setup
    set_mode(native, "sleep")
    runner.settings.harbor.wall_timeout_s = 0.2
    result = await runner.run(task_dir=task, attempt_dir=tmp_path / "attempt", run_id="run")
    assert result.outcome == "timeout"
    assert not result.passed
    assert result.trajectory_path == "trajectory.json"


async def test_cancellation_persists_result(runner_setup, tmp_path):
    runner, task, native = runner_setup
    set_mode(native, "sleep")
    run = asyncio.create_task(
        runner.run(task_dir=task, attempt_dir=tmp_path / "attempt", run_id="run")
    )
    for _ in range(100):
        if (tmp_path / "attempt/harbor/run/trial/result.json").exists():
            break
        await asyncio.sleep(0.01)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    result = json.loads((tmp_path / "attempt/result.json").read_text())
    assert result["outcome"] == "cancelled"
    assert result["trajectory_path"] == "trajectory.json"


def test_freeze_and_audited_constraint(runner_setup, tmp_path):
    _, _, source = runner_setup
    destination = tmp_path / "frozen.yaml"
    digest, base = freeze_config(source, destination, audited=True)
    assert len(digest) == 64 and base == str(tmp_path)
    source.write_text("agents: [{name: external}]\nverifier: {disable: true}\n")
    assert load_config(destination)["agents"][0]["import_path"] == "my.module:Agent"
    with pytest.raises(ValueError, match="verification"):
        load_config(source, audited=True)


async def test_guidance_uses_native_instruction_and_readonly_mount(runner_setup, tmp_path):
    runner, task, native = runner_setup
    guidance = tmp_path / "context"
    (guidance / "seed").mkdir(parents=True)
    (guidance / "seed/source.txt").write_text("authorized old seed")
    (guidance / "instruction.md").write_text("authorized method")
    (guidance / "manifest.json").write_text("{}")
    await runner.run(
        task_dir=task, attempt_dir=tmp_path / "attempt", run_id="run", guidance=guidance
    )
    config = yaml.safe_load((tmp_path / "attempt/harbor-config.yaml").read_text())
    assert config["extra_instruction_paths"] == [str(tmp_path / "attempt/guidance/instruction.md")]
    mount = config["environment"]["mounts"][0]
    assert mount["read_only"] and mount["target"] == "/opt/saf-guidance"
    assert mount["source"] == str(tmp_path / "attempt/guidance/seed")
    assert config["agents"] == load_config(native)["agents"]


@pytest.mark.parametrize("harness", ["codex", "terminus-2"])
async def test_native_harbor_accepts_generated_config(runner_setup, tmp_path, harness):
    import subprocess

    binary = Path(__file__).parents[2] / ".harbor-venv/bin/harbor"
    if not binary.exists():
        pytest.skip("optional independent Harbor environment not installed")
    runner, task, native = runner_setup
    # Known native harness for validation only; no job execution or model call.
    native.write_text(f"agents: [{{name: {harness}, model_name: test}}]\n")
    await runner.run(task_dir=task, attempt_dir=tmp_path / "attempt", run_id="run")
    result = await asyncio.to_thread(
        subprocess.run,
        [
            str(binary),
            "run",
            "--config",
            str(tmp_path / "attempt/harbor-config.yaml"),
            "--print-config",
            "--yes",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["agents"][0]["name"] == harness
    assert config["tasks"][0]["path"] == str(task)


async def test_termination_reaps_descendants_after_parent_exits(runner_setup, tmp_path):
    import contextlib
    import os
    import signal

    runner, _, _ = runner_setup
    ready = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "time.sleep(60)\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(parent_script),
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if ready.is_file():
                break
            await asyncio.sleep(0.01)
        child_pid = int(ready.read_text())
        await runner._terminate(process)
        for _ in range(100):
            status = Path(f"/proc/{child_pid}/stat")
            if not status.exists() or status.read_text().split()[2] == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("descendant survived process-group termination")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def test_artifact_diagnostics_preserve_timeout_cause(runner_setup, tmp_path):
    from science_agent_factory.contracts.state import RolloutResult

    runner, _, _ = runner_setup
    result = RolloutResult(outcome="timeout", error="Harbor exceeded configured wall_timeout_s")
    runner._read_artifacts(tmp_path, "missing-job", result)
    assert result.outcome == "timeout"
    assert "wall_timeout_s" in result.error
    assert "trial result" in result.error
