"""Builder Prompt 阶段路由测试。

测试目的：验证默认配置为 Builder 的Harbor 构建 Prompt 包含内部设计要求。
被测接口：``FactorySettings.load_default`` 及 ``AgentProfile.phase_prompt_files``。
输入与夹具：包内默认 YAML 与 Prompt 资源。
预期结果：构建阶段文件存在、内容保持 Seed 无关，Harbor Prompt 包含 Harbor 交付基础契约。
"""

from science_agent_factory import FactorySettings


def test_builder_phase_prompts_are_explicit_and_generic() -> None:
    profile = FactorySettings.load_default().agents["builder"]
    assert set(profile.phase_prompt_files) == {"harbor_build"}
    prompts = {
        name: path.read_text(encoding="utf-8") for name, path in profile.phase_prompt_files.items()
    }
    assert all(path.is_file() for path in profile.phase_prompt_files.values())
    assert "supraglacial" not in "".join(prompts.values()).lower()
    assert "task.toml" in prompts["harbor_build"]
    assert "outputs/task-metadata/design_card.json" in prompts["harbor_build"]
    assert "task-design-card.schema.json" in prompts["harbor_build"]
