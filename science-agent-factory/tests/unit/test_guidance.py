"""策略卡公开路径的边界回归测试。

测试目的：允许公开说明中的容器接口，拒绝未声明路径、私有路径和错误引用。
被测接口：validate_strategy_card。
输入与夹具：临时公开 instruction.md、私有知识目录与最小策略卡。
预期结果：公开输出路径通过；宿主路径、路径前缀扩展、私有标记和工作区引用被拒绝。
"""

import pytest

from science_agent_factory.contracts.deliveries import SolverStrategyCard
from science_agent_factory.workflow.guidance import validate_strategy_card


@pytest.mark.parametrize(
    ("path", "reference", "error"),
    [
        ("/root/model.onnx", "instruction.md", None),
        ("/root/model.onnx.private", "instruction.md", "undeclared absolute path"),
        ("/data/hy/private.json", "instruction.md", "undeclared absolute path"),
        ("/solution/solve.py", "instruction.md", "private marker"),
        ("/root/model.onnx", "inputs/task/instruction.md", "public reference is missing"),
    ],
)
def test_strategy_paths_follow_public_contract(tmp_path, path, reference, error):
    public = tmp_path / "public"
    public.mkdir()
    (public / "instruction.md").write_text(
        "Write `/root/model.onnx`. Private `/solution/solve.py` is unavailable."
    )
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    card = SolverStrategyCard(
        task_id="candidate-001",
        seed_id="seed",
        objective=f"Write {path}",
        task_difference="same scientific objective",
        method_steps=["fit a regularized model"],
        tool_patterns=["inspect tensor shapes"],
        decision_points=["compare validation errors"],
        invariants=["finite predictions"],
        failure_modes=["overfitting"],
        verification_strategy=["check held-out predictions"],
        public_contract_refs=[reference],
        excluded_instance_details=["fitted coefficients"],
    )
    kwargs = {
        "task_id": card.task_id, "seed_id": card.seed_id, "public_root": public,
        "knowledge_source": card.knowledge_source, "knowledge_root": knowledge,
    }
    if error:
        with pytest.raises(ValueError, match=error):
            validate_strategy_card(card, **kwargs)
    else:
        validate_strategy_card(card, **kwargs)
