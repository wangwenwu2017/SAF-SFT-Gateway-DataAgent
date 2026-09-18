"""Harbor 任务目录与 Builder 旁路元数据验证测试。

测试目的：确认 Builder 直接生成的 Harbor 1.4 Task 具备可执行入口、标准奖励出口和可移植环境，并确保
旁路元数据包含有效设计卡、只引用目录内证据、身份一致且 Seed 环境摘要可信。
被测接口：validate_harbor_task、validate_harbor_metadata、validate_harbor_delivery、harbor_task_fingerprint。
输入与夹具：临时 Harbor Task、合成与 Seed 复制两类 metadata、最小探索/环境/自测报告及故障变体。
预期结果：完整任务与证据索引通过；缺失结构、符号链接、凭据、本机镜像/路径、越界证据和错误 Seed
摘要被确定性拒绝，任务指纹随正式内容变化。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from science_agent_factory.agents.seed_environment import tree_digest
from science_agent_factory.contracts.design import TaskDesignCard
from science_agent_factory.verification.harbor_task import (
    harbor_task_fingerprint,
    validate_harbor_delivery,
    validate_harbor_metadata,
    validate_harbor_task,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _harbor_task(tmp_path: Path, task_id: str = "candidate-001") -> Path:
    root = tmp_path / "harbor-task"
    (root / "environment").mkdir(parents=True)
    (root / "solution").mkdir()
    (root / "tests").mkdir()
    (root / "instruction.md").write_text("实现一个可验证的科学计算程序。\n", encoding="utf-8")
    (root / "task.toml").write_text(
        f"""
schema_version = "1.4"

[task]
name = "science-agent-factory/{task_id}"
description = "Portable scientific task"

[agent]
timeout_sec = 120

[environment]
""".lstrip(),
        encoding="utf-8",
    )
    (root / "environment" / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /workspace\nCOPY . /workspace\n",
        encoding="utf-8",
    )
    solve = root / "solution" / "solve.sh"
    solve.write_text(
        "#!/bin/sh\nset -eu\nprintf '42\\n' > /workspace/answer.txt\n", encoding="utf-8"
    )
    solve.chmod(0o755)
    verifier = root / "tests" / "test.sh"
    verifier.write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\nprintf '1\\n' > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    return root


def _difficulty_assessment() -> dict[str, object]:
    dimensions = (
        "generality",
        "algorithmic_depth",
        "implementation_scope",
        "edge_case_coverage",
        "environment_tooling",
    )
    return {
        "policy": "preserve",
        "dimensions": [
            {
                "dimension": dimension,
                "seed_score": 1,
                "candidate_score": 1,
                "evidence": "The candidate preserves this dimension.",
            }
            for dimension in dimensions
        ],
        "hidden_generalization": True,
        "candidate_requires_executable_code": True,
        "verdict": "preserved",
        "summary": "The task preserves the seed difficulty.",
    }


def _design_card(task_id: str, seed_id: str, mapping: dict) -> dict:
    return TaskDesignCard(
        task_id=task_id,
        seed_id=seed_id,
        objective="compute",
        substantive_changes=["new problem"],
        seed_shortest_solution=["compute"],
        candidate_shortest_solution=["compute"],
        capability_mapping=mapping,
        difficulty_assessment=_difficulty_assessment(),
        acceptance_claims=[
            {
                "claim_id": "correct",
                "public_requirement": "correct output",
                "planned_verification": "compare values",
                "representative_invalid_solution": "constant",
                "rejection_evidence": "different cases",
            }
        ],
        adversarial_shortcuts=[
            {
                "shortcut_id": "constant",
                "description": "constant answer",
                "exploited_information": "example",
                "rejection_plan": "hidden case",
            }
        ],
        rejected_superficial_ideas=["rename"],
        implementation_risks=["precision"],
        summary="planned",
    ).model_dump(mode="json")


def _harbor_metadata(
    tmp_path: Path,
    *,
    task_id: str = "candidate-001",
    seed_id: str = "seed-001",
    environment_source: str = "synthesized",
    seed_digest: str | None = None,
) -> Path:
    root = tmp_path / "task-metadata"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    for name in ("baseline.txt", "environment.txt", "self-test.txt"):
        (evidence / name).write_text(f"evidence: {name}\n", encoding="utf-8")
    experiment = {
        "hypothesis": "The seed workflow can be reproduced.",
        "commands": ["python reproduce.py"],
        "outcome": "confirmed",
        "observation": "The expected result was reproduced.",
        "evidence_paths": ["evidence/baseline.txt"],
        "design_implication": "Preserve the executable scientific workflow.",
    }
    _write_json(
        root / "exploration_report.json",
        {
            "schema_version": "saf.exploration.v1",
            "seed_id": seed_id,
            "seed_reproduction": experiment,
            "experiments": [],
            "selected_insight": "A related scientific property can be tested.",
            "preserved_capabilities": ["Run and verify a scientific workflow."],
            "substantive_changes": ["Change the target property and hidden cases."],
            "rejected_superficial_ideas": ["Only rename the output file."],
            "difficulty_assessment": _difficulty_assessment(),
            "capability_mapping": {
                "schema_version": "saf.capability-mapping.v1",
                "entries": [
                    {
                        "mapping_id": "core-workflow",
                        "seed_capability": "Execute a scientific workflow.",
                        "candidate_capability": "Execute the related workflow.",
                        "relation": "preserved",
                        "core": True,
                        "evidence": "Both tasks require executable analysis.",
                    }
                ],
                "summary": "The core workflow remains represented.",
            },
        },
    )
    exploration = json.loads((root / "exploration_report.json").read_text())
    _write_json(
        root / "design_card.json",
        _design_card(task_id, seed_id, exploration["capability_mapping"]),
    )
    _write_json(
        root / "environment_report.json",
        {
            "schema_version": "saf.environment-run.v1",
            "backend": "docker",
            "detected_inputs": [],
            "start_commands": ["harbor run --agent oracle"],
            "experiment_commands": [],
            "resources": [],
            "evidence_paths": ["evidence/environment.txt"],
            "cleaned": True,
            "cleanup_summary": "All temporary containers were removed.",
        },
    )
    for name, reward in [("oracle", 1), ("nop", 0)]:
        _write_json(
            evidence / (name + ".json"),
            {
                "trial_name": name,
                "finished_at": "2026-09-17T00:00:00Z",
                "agent_info": {"name": name},
                "verifier_result": {"rewards": {"reward": reward}},
            },
        )
    _write_json(
        root / "self_test.json",
        {
            "schema_version": "saf.harbor-self-test.v2",
            "task_sha256": harbor_task_fingerprint(tmp_path / "harbor-task")
            if (tmp_path / "harbor-task").exists()
            else "a" * 64,
            "oracle_result_path": "evidence/oracle.json",
            "negative_result_path": "evidence/nop.json",
            "task_loadable": True,
            "oracle_reward": 1.0,
            "negative_reward": 0.0,
            "commands": ["harbor run --agent oracle", "harbor run --agent nop"],
            "evidence_paths": ["evidence/self-test.txt"],
            "cleaned": True,
        },
    )
    _write_json(
        root / "metadata.json",
        {
            "schema_version": "saf.harbor-task-metadata.v1",
            "task_id": task_id,
            "seed_id": seed_id,
            "environment_source": environment_source,
            "seed_environment_sha256": seed_digest,
            "exploration_report_path": "exploration_report.json",
            "environment_report_path": "environment_report.json",
            "self_test_report_path": "self_test.json",
        },
    )
    return root


def _seed_bundle(tmp_path: Path, seed_id: str = "seed-001") -> tuple[Path, str]:
    root = tmp_path / "seed"
    environment = root / "environment"
    environment.mkdir(parents=True)
    (environment / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (root / "task_card.json").write_text("{}\n", encoding="utf-8")
    (root / "solution.txt").write_text("reference\n", encoding="utf-8")
    _write_json(
        root / "seed_bundle.json",
        {
            "schema_version": "saf.seed.v2",
            "seed_id": seed_id,
            "task_card": "task_card.json",
            "successful_trajectory": None,
            "solution_path": "solution.txt",
            "environment_dir": "environment",
            "input_dir": None,
            "software_dir": None,
            "notes": "",
        },
    )
    return root, tree_digest(environment)


def test_complete_harbor_task_is_portable_and_has_stable_identity(
    tmp_path: Path,
) -> None:
    task = _harbor_task(tmp_path)

    info = validate_harbor_task(task, expected_task_id="candidate-001")
    first_fingerprint = harbor_task_fingerprint(task)
    (task / "instruction.md").write_text("实现一个不同的可验证科学计算程序。\n", encoding="utf-8")

    assert info.task_id == "candidate-001"
    assert info.title == "Portable scientific task"
    assert info.timeout_s == 120
    assert len(first_fingerprint) == 64
    assert harbor_task_fingerprint(task) != first_fingerprint


def test_harbor_task_requires_complete_structure_and_standard_reward(
    tmp_path: Path,
) -> None:
    task = _harbor_task(tmp_path)
    (task / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="standard verifier reward"):
        validate_harbor_task(task)

    incomplete_task = _harbor_task(tmp_path / "missing-environment")
    (incomplete_task / "environment" / "Dockerfile").unlink()
    with pytest.raises(FileNotFoundError, match="Dockerfile or Compose"):
        validate_harbor_task(incomplete_task)


def test_harbor_task_accepts_variable_based_standard_reward_path(
    tmp_path: Path,
) -> None:
    task = _harbor_task(tmp_path)
    (task / "tests" / "test.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "logdir=/logs/verifier\n"
        'mkdir -p "$logdir"\n'
        "printf '1\\n' > \"$logdir/reward.txt\"\n",
        encoding="utf-8",
    )
    (task / "tests" / "test.sh").chmod(0o755)

    validate_harbor_task(task)


@pytest.mark.parametrize(
    "reference",
    ["/solution/solve.sh", "/app/solution/solve.sh", "solution/solve.sh"],
)
def test_harbor_task_rejects_verifier_solution_reference(tmp_path: Path, reference: str) -> None:
    task = _harbor_task(tmp_path)
    verifier = task / "tests" / "test.sh"
    verifier.write_text(
        "#!/bin/sh\nset -eu\n"
        f"cp {reference} /tmp/reference\n"
        "mkdir -p /logs/verifier\nprintf '1\n' > /logs/verifier/reward.txt\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)

    with pytest.raises(ValueError, match="cannot reference"):
        validate_harbor_task(task)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("local-image", "cannot depend on docker_image"),
        ("absolute-bind", "absolute host bind mount"),
        ("parent-copy", "cannot copy parent"),
        ("credential", "credential file"),
    ],
)
def test_harbor_task_rejects_nonportable_environment(
    tmp_path: Path, fault: str, message: str
) -> None:
    task = _harbor_task(tmp_path)
    if fault == "local-image":
        config = task / "task.toml"
        config.write_text(
            config.read_text(encoding="utf-8") + 'docker_image = "local/prewarmed:latest"\n',
            encoding="utf-8",
        )
    elif fault == "absolute-bind":
        (task / "environment" / "compose.yaml").write_text(
            "services:\n  worker:\n    build: .\n    volumes:\n      - /srv/private:/private\n",
            encoding="utf-8",
        )
    elif fault == "parent-copy":
        dockerfile = task / "environment" / "Dockerfile"
        dockerfile.write_text(
            dockerfile.read_text(encoding="utf-8") + "COPY ../secret /secret\n",
            encoding="utf-8",
        )
    else:
        (task / "environment" / ".env").write_text("TOKEN=must-not-ship\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_harbor_task(task)


def test_harbor_task_rejects_symlinked_content(tmp_path: Path) -> None:
    task = _harbor_task(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("host content\n", encoding="utf-8")
    (task / "environment" / "linked.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        validate_harbor_task(task)


@pytest.mark.parametrize(
    "source",
    ["public_tests/", "foo/tests/fixture.txt", "solutions/fixture.txt"],
)
def test_harbor_task_allows_non_forbidden_copy_source_names(tmp_path: Path, source: str) -> None:
    task = _harbor_task(tmp_path)
    dockerfile = task / "environment" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + f"COPY {source} /workspace/input\n",
        encoding="utf-8",
    )

    validate_harbor_task(task)


@pytest.mark.parametrize("source", ["solution/", "tests/", "./tests/", "../secret"])
def test_harbor_task_rejects_top_level_solution_tests_or_parent_copy(
    tmp_path: Path, source: str
) -> None:
    task = _harbor_task(tmp_path)
    dockerfile = task / "environment" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + f"COPY {source} /workspace/input\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot copy parent, solution, or tests"):
        validate_harbor_task(task)


def test_harbor_task_allows_multiline_copy_sources(tmp_path: Path) -> None:
    task = _harbor_task(tmp_path)
    dockerfile = task / "environment" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + "COPY public_tests/ public_input/ "
        + "\\\n"
        + "  fixture.txt /workspace/input/\n",
        encoding="utf-8",
    )

    validate_harbor_task(task)


def test_harbor_task_rejects_forbidden_multiline_copy_source(tmp_path: Path) -> None:
    task = _harbor_task(tmp_path)
    dockerfile = task / "environment" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8")
        + "COPY public_input/ "
        + "\\\n"
        + "  tests/fixture.txt /workspace/input/\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot copy parent, solution, or tests"):
        validate_harbor_task(task)


def test_synthesized_harbor_metadata_validates_all_evidence(tmp_path: Path) -> None:
    metadata_dir = _harbor_metadata(tmp_path)

    metadata = validate_harbor_metadata(
        metadata_dir,
        expected_task_id="candidate-001",
        expected_seed_id="seed-001",
        environment_strategy="synthesize",
        seed_dir=tmp_path / "unused-seed",
    )

    assert metadata.environment_source == "synthesized"
    assert metadata.seed_environment_sha256 is None


def test_harbor_metadata_rejects_path_escape_and_missing_evidence(
    tmp_path: Path,
) -> None:
    metadata_dir = _harbor_metadata(tmp_path)
    metadata_path = metadata_dir / "metadata.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["exploration_report_path"] = "../outside.json"
    _write_json(metadata_path, payload)

    with pytest.raises(ValueError, match="path must stay inside"):
        validate_harbor_metadata(
            metadata_dir,
            expected_task_id="candidate-001",
            expected_seed_id="seed-001",
            environment_strategy="synthesize",
            seed_dir=tmp_path / "unused-seed",
        )

    payload["exploration_report_path"] = "exploration_report.json"
    _write_json(metadata_path, payload)
    (metadata_dir / "evidence" / "self-test.txt").unlink()
    with pytest.raises(FileNotFoundError, match="metadata file is missing"):
        validate_harbor_metadata(
            metadata_dir,
            expected_task_id="candidate-001",
            expected_seed_id="seed-001",
            environment_strategy="synthesize",
            seed_dir=tmp_path / "unused-seed",
        )


def test_seed_copy_metadata_requires_current_environment_digest(
    tmp_path: Path,
) -> None:
    seed_dir, digest = _seed_bundle(tmp_path)
    metadata_dir = _harbor_metadata(
        tmp_path,
        environment_source="seed_copy",
        seed_digest=digest,
    )

    metadata = validate_harbor_metadata(
        metadata_dir,
        expected_task_id="candidate-001",
        expected_seed_id="seed-001",
        environment_strategy="reuse_seed",
        seed_dir=seed_dir,
    )
    assert metadata.seed_environment_sha256 == digest

    (seed_dir / "environment" / "new-data.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_harbor_metadata(
            metadata_dir,
            expected_task_id="candidate-001",
            expected_seed_id="seed-001",
            environment_strategy="reuse_seed",
            seed_dir=seed_dir,
        )


def test_harbor_delivery_reports_independent_contract_errors(tmp_path: Path) -> None:
    task = _harbor_task(tmp_path)
    task_config = task / "task.toml"
    task_config.write_text(
        task_config.read_text(encoding="utf-8").replace(
            "science-agent-factory/candidate-001", "bad-name"
        ),
        encoding="utf-8",
    )
    metadata = _harbor_metadata(tmp_path)
    (metadata / "evidence" / "baseline.txt").unlink()
    seed, _ = _seed_bundle(tmp_path)

    with pytest.raises(ValueError) as exc_info:
        validate_harbor_delivery(
            task,
            metadata,
            expected_task_id="candidate-001",
            expected_seed_id="seed-001",
            environment_strategy="synthesize",
            seed_dir=seed,
        )

    message = str(exc_info.value)
    assert "task:" in message
    assert "metadata:" in message


@pytest.mark.parametrize(
    "fault",
    ["stale-task", "wrong-reward", "wrong-agent", "missing-result", "unfinished", "malformed"],
)
def test_self_test_is_bound_to_actual_trial_and_revision(tmp_path, fault):
    task = _harbor_task(tmp_path)
    metadata = _harbor_metadata(tmp_path)
    seed, _ = _seed_bundle(tmp_path)
    kwargs = {
        "expected_task_id": "candidate-001",
        "expected_seed_id": "seed-001",
        "environment_strategy": "synthesize",
        "seed_dir": seed,
    }
    validate_harbor_delivery(task, metadata, **kwargs)
    oracle = metadata / "evidence/oracle.json"
    result = json.loads(oracle.read_text())
    if fault == "stale-task":
        (task / "instruction.md").write_text("changed task")
    elif fault == "wrong-reward":
        result["verifier_result"]["rewards"]["reward"] = 0
    elif fault == "wrong-agent":
        result["agent_info"]["name"] = "some-agent"
    elif fault == "unfinished":
        result["finished_at"] = None
    elif fault == "malformed":
        result["agent_info"] = []
    _write_json(oracle, result)
    if fault == "missing-result":
        oracle.unlink()
    with pytest.raises((ValueError, TypeError, FileNotFoundError)):
        validate_harbor_delivery(task, metadata, **kwargs)


@pytest.mark.parametrize("defect", ["missing", "schema", "task_id", "seed_id"])
def test_metadata_requires_valid_current_design_card(tmp_path: Path, defect: str) -> None:
    metadata = _harbor_metadata(tmp_path)
    card_path = metadata / "design_card.json"
    card = json.loads(card_path.read_text())
    if defect == "missing":
        card_path.unlink()
    elif defect == "schema":
        card.pop("acceptance_claims")
        _write_json(card_path, card)
    else:
        card[defect] = "another-identity"
        _write_json(card_path, card)
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_harbor_metadata(
            metadata,
            expected_task_id="candidate-001",
            expected_seed_id="seed-001",
            environment_strategy="synthesize",
            seed_dir=tmp_path / "unused-seed",
        )
