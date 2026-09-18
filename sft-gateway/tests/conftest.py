"""隔离测试环境；使用虚构管理凭据，不访问实际上游或本机采集数据库。"""
import pytest

@pytest.fixture(autouse=True)
def isolated_gateway_environment(monkeypatch, tmp_path):
    """为每个测试提供独立数据库和管理 key；具体认证用例可覆盖。"""
    for name in ("SFT_UPSTREAM_API_KEY", "SFT_GATEWAY_API_KEY", "SFT_RAW_ARCHIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SFT_ADMIN_API_KEY", "test-admin")
    monkeypatch.setenv("SFT_DB", str(tmp_path / "default.sqlite"))
