"""S2.10-0.1 · Snapshot + Diff 纯函数测试(零网络零 I/O)。

守 spec「动态 diff 抓新免费模型」数据层契约:
- DiscoveredModel/Snapshot/DiffResult immutable(守 coding-style)
- diff_snapshots 按 model_id 主键(跨 display_name/tier 抖动稳定)
- 首次轮询 prev=empty → added=curr 全部
- 跨 source 比较 fail-loud(ValueError)
- 红线:diff 是发现信号,added 还要过面试(0.4),不直接进排序键(本片零副作用)
"""
from __future__ import annotations

import pytest

from llm_router.scanner.snapshot import (
    DiffResult,
    DiscoveredModel,
    ScannerSource,
    Snapshot,
    diff_snapshots,
    merge_snapshots,
)


def _nv(mid: str, tier: str | None = None, name: str | None = None) -> DiscoveredModel:
    return DiscoveredModel(
        source=ScannerSource.NVIDIA, model_id=mid, display_name=name, tier=tier
    )


def _or(mid: str, tier: str | None = None, name: str | None = None) -> DiscoveredModel:
    return DiscoveredModel(
        source=ScannerSource.OPENROUTER, model_id=mid, display_name=name, tier=tier
    )


class TestDiscoveredModel:
    def test_frozen_immutable(self):
        m = _nv("nvidia/llama-3.1-nemotron-70b-instruct")
        with pytest.raises(Exception):
            m.model_id = "x"  # type: ignore[misc]

    def test_name_falls_back_to_model_id(self):
        m = _nv("nvidia/foo", name=None)
        assert m.name == "nvidia/foo"

    def test_name_uses_display_name_when_set(self):
        m = _nv("nvidia/foo", name="Nemotron 70B")
        assert m.name == "Nemotron 70B"

    def test_hashable_for_set_dedup(self):
        a = _nv("nvidia/foo", tier="strong")
        b = _nv("nvidia/foo", tier="strong")
        assert {a, b} == {a}  # 等值去重


class TestSnapshot:
    def test_empty_has_zero_models(self):
        snap = Snapshot.empty(ScannerSource.NVIDIA)
        assert len(snap) == 0
        assert snap.model_ids() == frozenset()

    def test_model_ids_extracts_ids(self):
        snap = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a"), _nv("b"), _nv("c")}),
        )
        assert snap.model_ids() == frozenset({"a", "b", "c"})

    def test_taken_at_default_is_iso(self):
        snap = Snapshot(source=ScannerSource.NVIDIA, models=frozenset())
        assert snap.taken_at  # 非空
        assert "T" in snap.taken_at  # ISO 8601 含 T


class TestDiffSnapshots:
    def test_first_poll_prev_empty_all_added(self):
        prev = Snapshot.empty(ScannerSource.NVIDIA)
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a"), _nv("b")}),
        )
        diff = diff_snapshots(prev, curr)
        assert diff.source is ScannerSource.NVIDIA
        assert {m.model_id for m in diff.added} == {"a", "b"}
        assert diff.removed == frozenset()
        assert diff.unchanged == frozenset()

    def test_stable_models_in_unchanged(self):
        prev = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a"), _nv("b")}),
        )
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a"), _nv("b"), _nv("c")}),
        )
        diff = diff_snapshots(prev, curr)
        assert {m.model_id for m in diff.added} == {"c"}
        assert diff.removed == frozenset()
        assert {m.model_id for m in diff.unchanged} == {"a", "b"}

    def test_removed_models_detected(self):
        prev = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a"), _nv("b"), _nv("c")}),
        )
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a")}),
        )
        diff = diff_snapshots(prev, curr)
        assert diff.added == frozenset()
        assert {m.model_id for m in diff.removed} == {"b", "c"}
        assert {m.model_id for m in diff.unchanged} == {"a"}

    def test_diff_by_model_id_not_display_name(self):
        """同 model_id 但 display_name 抖动不算 added/removed(按 id 主键稳定)。"""
        prev = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", name="Old Name")}),
        )
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", name="New Name")}),
        )
        diff = diff_snapshots(prev, curr)
        assert diff.added == frozenset()
        assert diff.removed == frozenset()
        assert {m.model_id for m in diff.unchanged} == {"a"}
        # unchanged 取 curr 最新字段(display_name 已更新)
        assert next(iter(diff.unchanged)).name == "New Name"

    def test_diff_by_model_id_not_tier(self):
        """同 model_id 但 tier 推断抖动不算 added/removed。"""
        prev = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", tier="strong")}),
        )
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", tier="medium")}),
        )
        diff = diff_snapshots(prev, curr)
        assert diff.added == frozenset()
        assert diff.removed == frozenset()
        assert {m.model_id for m in diff.unchanged} == {"a"}

    def test_cross_source_raises_value_error(self):
        """跨 source 比较 fail-loud(不静默混比)。"""
        prev = Snapshot.empty(ScannerSource.NVIDIA)
        curr = Snapshot(
            source=ScannerSource.OPENROUTER,
            models=frozenset({_or("a")}),
        )
        with pytest.raises(ValueError, match="跨 source"):
            diff_snapshots(prev, curr)

    def test_added_takes_curr_full_model(self):
        """added 返回 curr 的完整 DiscoveredModel(供下游面试用全字段)。"""
        prev = Snapshot.empty(ScannerSource.NVIDIA)
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", tier="strong", name="Foo")}),
        )
        diff = diff_snapshots(prev, curr)
        added = next(iter(diff.added))
        assert added.model_id == "a"
        assert added.tier == "strong"
        assert added.name == "Foo"
        assert added.source is ScannerSource.NVIDIA

    def test_removed_takes_prev_full_model(self):
        """removed 返回 prev 的完整 DiscoveredModel(供过期清退用原入库字段)。"""
        prev = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a", tier="strong", name="Foo")}),
        )
        curr = Snapshot.empty(ScannerSource.NVIDIA)
        diff = diff_snapshots(prev, curr)
        removed = next(iter(diff.removed))
        assert removed.model_id == "a"
        assert removed.tier == "strong"
        assert removed.name == "Foo"

    def test_both_empty(self):
        prev = Snapshot.empty(ScannerSource.NVIDIA)
        curr = Snapshot.empty(ScannerSource.NVIDIA)
        diff = diff_snapshots(prev, curr)
        assert diff.added == frozenset()
        assert diff.removed == frozenset()
        assert diff.unchanged == frozenset()

    def test_diff_result_is_immutable(self):
        prev = Snapshot.empty(ScannerSource.NVIDIA)
        curr = Snapshot(
            source=ScannerSource.NVIDIA,
            models=frozenset({_nv("a")}),
        )
        diff = diff_snapshots(prev, curr)
        assert isinstance(diff, DiffResult)
        with pytest.raises(Exception):
            diff.added = frozenset()  # type: ignore[misc]


class TestMergeSnapshots:
    def test_groups_by_source(self):
        nv = Snapshot(source=ScannerSource.NVIDIA, models=frozenset({_nv("a")}))
        orr = Snapshot(source=ScannerSource.OPENROUTER, models=frozenset({_or("x")}))
        out = merge_snapshots([nv, orr])
        assert set(out) == {ScannerSource.NVIDIA, ScannerSource.OPENROUTER}
        assert {m.model_id for m in out[ScannerSource.NVIDIA]} == {"a"}
        assert {m.model_id for m in out[ScannerSource.OPENROUTER]} == {"x"}

    def test_latest_wins_on_duplicate_source(self):
        """同 source 多次出现 → 后者覆盖(latest-wins)。"""
        old = Snapshot(source=ScannerSource.NVIDIA, models=frozenset({_nv("a")}))
        new = Snapshot(source=ScannerSource.NVIDIA, models=frozenset({_nv("b"), _nv("c")}))
        out = merge_snapshots([old, new])
        assert {m.model_id for m in out[ScannerSource.NVIDIA]} == {"b", "c"}

    def test_empty_input(self):
        assert merge_snapshots([]) == {}
