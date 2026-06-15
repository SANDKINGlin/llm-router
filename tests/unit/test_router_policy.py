"""S1.3 · router-policy.yaml schema 加载与校验(CI 门禁核心)。

守两条验收:
  - 验收①:合法 yaml 经 load_policy() 加载,pydantic 校验通过,ProviderEntry 字段正确解析
  - 验收②(CI 门禁):畸形 yaml(tier 非枚举 / 必填字段缺失)→ pydantic ValidationError

config.py surgical 扩展验证:load_policy/policy 缓存逻辑不变(零回归)。
TDD:本套件先 RED——ProviderEntry 未实现时 import 即失败。
"""
from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from llm_router.config import ProviderEntry, load_policy

_VALID_YAML = """\
version: 1
policy_version: "0.1.0"
gray_percent: 100
providers:
  - name: mock
    tier: fast
    quota: 1000000
    cooldown_s: 30
tiers:
  strong: []
  medium: []
  fast: [mock]
"""


def test_valid_policy_loads_and_validates(tmp_path):
    """验收①:合法 yaml → load_policy 正常,ProviderEntry 字段正确解析。"""
    p = tmp_path / "router-policy.yaml"
    p.write_text(_VALID_YAML)

    policy = load_policy(p)

    assert policy.version == 1
    assert policy.policy_version == "0.1.0"
    assert policy.gray_percent == 100
    assert len(policy.providers) == 1

    entry = policy.providers[0]
    assert isinstance(entry, ProviderEntry)
    assert entry.name == "mock"
    assert entry.tier == "fast"
    assert entry.quota == 1_000_000
    assert entry.cooldown_s == 30
    # base_url / api_key_env 留空(Phase1 mock):Optional 默认 None
    assert entry.base_url is None
    assert entry.api_key_env is None

    assert policy.tiers["fast"] == ["mock"]


def test_malformed_policy_bad_tier_rejected(tmp_path):
    """验收②(CI 门禁):tier 非 strong/medium/fast → ValidationError。

    pydantic Literal 校验枚举——这是 CI schema 门禁的核心拦截点。
    """
    bad = _VALID_YAML.replace("tier: fast", "tier: ultra")
    p = tmp_path / "router-policy.yaml"
    p.write_text(bad)

    with pytest.raises(ValidationError):
        load_policy(p)


def test_malformed_policy_missing_required_field_rejected(tmp_path):
    """验收②(CI 门禁):provider 缺必填字段(无 tier)→ ValidationError。"""
    bad = textwrap.dedent(
        """\
        version: 1
        providers:
          - name: mockless
            quota: 100
            cooldown_s: 5
        tiers: {}
        """
    )
    p = tmp_path / "router-policy.yaml"
    p.write_text(bad)

    with pytest.raises(ValidationError):
        load_policy(p)
