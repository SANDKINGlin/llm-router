"""R37: 动态 adapter quota 按 source 调 端到端测试 (治本 scanner/dynamic.py).

R37 实施 (跟 R30/R32/R34 治本 precedent 6/5 同款):
- scanner/dynamic.py: 加 _DYNAMIC_QUOTA_BY_SOURCE dict (ScannerSource.NVIDIA=5000, OPENROUTER=1000000)
- scanner/dynamic.py: 加 _get_quota_for_source(source: ScannerSource) -> int 函数
- scanner/dynamic.py: 改 dynamic_entry_to_provider_entry 用 _get_quota_for_source(model.source) 替代 _DYNAMIC_QUOTA 写死
- tests/unit/scanner/test_dynamic.py: 改 test_fields_correct_free_zero_cost 期望新 quota (NVIDIA=5000)

注: ScannerSource 仅 NVIDIA + OPENROUTER 2 成员 (snapshot.py:27-31), 实测.
   旧 test 用 _nv() 工厂函数 (helper) 创 DiscoveredModel 实例, 跟 R34 fresh_key_manager fixture 同模式.
"""
import pytest

from llm_router.scanner.dynamic import (
    _get_quota_for_source,
    _DYNAMIC_QUOTA_BY_SOURCE,
    _DYNAMIC_QUOTA_DEFAULT,
    _DYNAMIC_QUOTA,
    dynamic_entry_to_provider_entry,
)
from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource


class TestGetQuotaForSource:
    """R37 治本核心: _get_quota_for_source 查表函数."""

    def test_nvidia_source_returns_5000(self):
        """R37: ScannerSource.NVIDIA → 5000 (NIM 免费档 ~5000/h, 跟 _DYNAMIC_QUOTA 写死 500000 不同)."""
        quota = _get_quota_for_source(ScannerSource.NVIDIA)
        assert quota == 5000
        assert quota != _DYNAMIC_QUOTA  # 不再写死 500000

    def test_openrouter_source_returns_1000000(self):
        """R37: ScannerSource.OPENROUTER → 1000000 (free 档, 跟 NVIDIA 差异 200x)."""
        quota = _get_quota_for_source(ScannerSource.OPENROUTER)
        assert quota == 1000000

    def test_quota_table_has_both_real_sources(self):
        """R37: _DYNAMIC_QUOTA_BY_SOURCE 含 2 个 ScannerSource 成员 (无臆造, 实测)."""
        assert ScannerSource.NVIDIA in _DYNAMIC_QUOTA_BY_SOURCE
        assert ScannerSource.OPENROUTER in _DYNAMIC_QUOTA_BY_SOURCE
        # 仅 2 成员, 跟 snapshot.py:27-31 一致
        assert len(_DYNAMIC_QUOTA_BY_SOURCE) == 2

    def test_quota_default_matches_dynamic_quota_constant(self):
        """R37: _DYNAMIC_QUOTA_DEFAULT 跟 _DYNAMIC_QUOTA 写死一致 (向后兼容).

        未来 ScannerSource 加新成员时, _get_quota_for_source 走 fallback 返回 500000.
        """
        assert _DYNAMIC_QUOTA_DEFAULT == _DYNAMIC_QUOTA == 500000


class TestDynamicEntryToProviderEntryR37:
    """R37 端到端: dynamic_entry_to_provider_entry 用 _get_quota_for_source."""

    def test_nvidia_model_gets_nvidia_quota(self):
        """R37: DiscoveredModel(NVIDIA) → dynamic_entry_to_provider_entry → quota=5000."""
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="llama-3.1-70b", tier="strong")
        e = dynamic_entry_to_provider_entry(m)
        assert e.quota == 5000
        assert e.quota != _DYNAMIC_QUOTA  # 不再 500000

    def test_openrouter_model_gets_openrouter_quota(self):
        """R37: DiscoveredModel(OPENROUTER) → quota=1000000."""
        m = DiscoveredModel(source=ScannerSource.OPENROUTER, model_id="gpt-oss-120b:free", tier="medium")
        e = dynamic_entry_to_provider_entry(m)
        assert e.quota == 1000000
        assert e.quota != _DYNAMIC_QUOTA  # 不再 500000
