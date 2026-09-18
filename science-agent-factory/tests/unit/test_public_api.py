"""当前接口与不兼容边界。

测试目的：配置只拥有内部三角色，外部 harness 由原生文件决定。
被测接口：FactorySettings、JobState、CLI。
输入与夹具：默认配置和临时 YAML。
预期结果：相对路径正确解析，旧字段和状态被拒绝。
"""

from pathlib import Path

import pytest

from science_agent_factory import BatchController, FactorySettings, ScienceAgentFactory
from science_agent_factory.cli import _parser
from science_agent_factory.contracts.state import CandidateState, JobState


def test_defaults():
    settings = FactorySettings.load_default()
    assert not settings.allow_host_full_access
    assert settings.pipeline.workflow_mode == "audited"
    assert settings.harbor.config_file is None
    assert set(settings.agents) == {"builder", "distiller", "auditor"}
    assert all(p.prompt_file.is_file() for p in settings.agents.values())
    assert ScienceAgentFactory and BatchController


def test_native_config_reference(tmp_path: Path):
    config = tmp_path / "factory.yaml"
    config.write_text(
        "harbor:\n  config_file: ./native.yaml\npipeline:\n  workflow_mode: audited\n"
    )
    settings = FactorySettings.load(config)
    assert settings.harbor.config_file == tmp_path / "native.yaml"
    assert settings.pipeline.workflow_mode == "audited"


@pytest.mark.parametrize(
    "text",
    [
        "harbor:\n  agent: codex\n",
        "codex:\n  gateway_capture: {enabled: true}\n",
        "codex:\n  gateway_retries: 1\n",
        "pipeline:\n  output_format: harbor\n",
        "pipeline:\n  max_design_revisions: 1\n",
        "agents:\n  builder:\n    phase_prompt_files: {design: obsolete.md}\n",
        "agents:\n  auditor:\n    phase_prompt_files: {design: obsolete.md}\n",
        "pipeline:\n  workflow_mode: lightweight_terminus\n",
        "batch:\n  target_accepted: 10\n",
    ],
)
def test_old_config_is_rejected(tmp_path, text):
    config = tmp_path / "old.yaml"
    config.write_text(text)
    with pytest.raises(ValueError):
        FactorySettings.load(config)


@pytest.mark.parametrize("schema_version", ["saf.job.v1", "saf.job.v3"])
def test_old_state_is_rejected(schema_version):
    with pytest.raises(ValueError):
        JobState.model_validate({"schema_version": schema_version, "job_id": "old"})


def test_cli():
    parser = _parser()
    assert parser.parse_args(["export", "job-1"]).format == "harbor"
    assert parser.parse_args(["batch-supervise", "batch-1"]).batch_id == "batch-1"
    with pytest.raises(SystemExit):
        parser.parse_args(["export", "job-1", "--format", "bundle"])


@pytest.mark.parametrize("stage", ["design_proposed", "design_accepted"])
def test_removed_design_stages_are_rejected(stage):
    with pytest.raises(ValueError):
        CandidateState(candidate_id="candidate-001", stage=stage)


@pytest.mark.parametrize("field", ["design_revision", "design_card_path", "design_review"])
def test_removed_design_fields_are_rejected(field):
    with pytest.raises(ValueError):
        CandidateState.model_validate({"candidate_id": "candidate-001", field: None})
