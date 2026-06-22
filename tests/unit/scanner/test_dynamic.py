"""S2.10-0.5 · DynamicScanner 编排器 + 侧挂循环 + 候选池桥测试(全注入,零网络)。

守 spec「动态 diff 抓新免费模型」端到端编排契约:
- tick():poll→diff→面试→入库→清退,返回 TickResult(各 source 统计)
- 首次轮询 prev=empty → added=curr 全部;合格入 active,失败不入
- 第二次轮询 removed → expire;unchanged 不动
- 无 probe_factory → 只 diff+存快照,不入池(只读模式)
- poll 失败 → TickResult.ok=False(error 记类型),不抛
- run_loop:stop_event 优雅退出,tick 异常不崩
- build_dynamic_adapters:active_models→候选三元组,缺 key 跳过,name 稳定唯一
- 红线:不接 production Cascade(Phase B);排序键字典序不变
"""
from __future__ import annotations

import asyncio

from llm_router.scanner.dynamic import (
    DynamicScanner,
    SourceTickStats,
    TickResult,
    build_dynamic_adapters,
    build_dynamic_entries,
    dynamic_entry_to_provider_entry,
)
from llm_router.scanner.snapshot import DiscoveredModel, ScannerSource, Snapshot
from llm_router.store.scanner_store import ScannerStore


def _run(coro):
    return asyncio.run(coro)


def _nv(mid, tier="strong"):
    return DiscoveredModel(source=ScannerSource.NVIDIA, model_id=mid, tier=tier)


def _or(mid, tier="strong"):
    return DiscoveredModel(source=ScannerSource.OPENROUTER, model_id=mid, tier=tier)


def _fetcher_returning(nv_models=None, or_models=None):
    """造 fetcher:按 URL 返回对应 source 的 /models payload。"""
    nv_payload = {"data": [{"id": m.model_id, "name": m.model_id} for m in (nv_models or [])]}
    or_payload = {"data": [{"id": m.model_id, "name": m.model_id} for m in (or_models or [])]}

    async def fetch(url, headers, timeout):
        if "nvidia" in url:
            return nv_payload
        return or_payload
    return fetch


def _probe_factory_passing():
    """造 probe_factory:每个 model 返回一个永远合格的 probe。"""
    def factory(model):
        async def probe(model_id):
            return "PONG"
        return probe
    return factory


def _probe_factory_failing():
    def factory(model):
        async def probe(model_id):
            raise RuntimeError("net down")
        return probe
    return factory


def _probe_factory_empty():
    def factory(model):
        async def probe(model_id):
            return "   "  # 空内容
        return probe
    return factory


# ── tick() 首次轮询 ───────────────────────────────────────────────

class TestTickFirstPoll:
    def test_first_poll_all_added_interviewed_passed(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b"), _nv("b-large")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                assert result.ok is True
                nv = result.stats[ScannerSource.NVIDIA]
                assert nv.added == 2
                assert nv.interviewed == 2
                assert nv.passed == 2
                assert nv.removed == 0
                assert nv.expired == 0
                # 入库 active
                active = await store.active_models()
                assert {m.model_id for m in active} == {"a-70b", "b-large"}
            finally:
                await store.close()
        _run(body())

    def test_first_poll_no_probe_factory_only_snapshots(self, tmp_path):
        """无 probe_factory → 只 diff+存快照,不入池(只读模式)。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=None,
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                assert result.ok is True
                nv = result.stats[ScannerSource.NVIDIA]
                assert nv.added == 1
                assert nv.interviewed == 0  # 无 probe,不面试
                assert nv.passed == 0
                assert len(await store.active_models()) == 0  # 不入池
                # 但快照已存
                snap = await store.load_snapshot(ScannerSource.NVIDIA)
                assert snap is not None and snap.model_ids() == frozenset({"a-70b"})
            finally:
                await store.close()
        _run(body())

    def test_interview_failure_not_entered(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_failing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                nv = result.stats[ScannerSource.NVIDIA]
                assert nv.interviewed == 1
                assert nv.passed == 0  # 面试失败不入池
                assert len(await store.active_models()) == 0
            finally:
                await store.close()
        _run(body())

    def test_empty_content_interview_failure(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_empty(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                assert result.stats[ScannerSource.NVIDIA].passed == 0
                assert len(await store.active_models()) == 0
            finally:
                await store.close()
        _run(body())


# ── tick() 第二次轮询:diff removed → expire ──────────────────────

class TestTickDiffRemoved:
    def test_removed_models_expired(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b"), _nv("b-large")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                await ds.tick()  # 首次:a,b 入池

                # 切 fetcher:只剩 a(b 下架)
                ds._fetcher = _fetcher_returning(nv_models=[_nv("a-70b")])
                result = await ds.tick()
                nv = result.stats[ScannerSource.NVIDIA]
                assert nv.added == 0
                assert nv.removed == 1  # b 下架
                assert nv.expired == 1  # b 从 active 清退
                active = await store.active_models()
                assert {m.model_id for m in active} == {"a-70b"}  # b 已 expire
            finally:
                await store.close()
        _run(body())

    def test_unchanged_models_not_re_interviewed(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                call_count = {"n": 0}

                def factory(model):
                    async def probe(model_id):
                        call_count["n"] += 1
                        return "ok"
                    return probe

                ds = DynamicScanner(
                    store,
                    probe_factory=factory,
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                await ds.tick()  # 首次面试 a 1 次
                assert call_count["n"] == 1
                await ds.tick()  # 第二次 a unchanged,不重面试
                assert call_count["n"] == 1  # 没增加
            finally:
                await store.close()
        _run(body())

    def test_new_model_added_on_second_poll(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                await ds.tick()
                ds._fetcher = _fetcher_returning(nv_models=[_nv("a-70b"), _nv("c-70b")])
                result = await ds.tick()
                nv = result.stats[ScannerSource.NVIDIA]
                assert nv.added == 1  # c 新增
                assert nv.removed == 0
                active = await store.active_models()
                assert {m.model_id for m in active} == {"a-70b", "c-70b"}
            finally:
                await store.close()
        _run(body())


# ── 多 source ─────────────────────────────────────────────────────

class TestTickMultiSource:
    def test_both_sources_processed(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")], or_models=[_or("x:free")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                assert set(result.stats) == {ScannerSource.NVIDIA, ScannerSource.OPENROUTER}
                assert result.stats[ScannerSource.NVIDIA].added == 1
                assert result.stats[ScannerSource.OPENROUTER].added == 1
                active = await store.active_models()
                assert {m.model_id for m in active} == {"a-70b", "x:free"}
            finally:
                await store.close()
        _run(body())

    def test_missing_key_source_empty_snapshot(self, tmp_path):
        """缺 key 的 source → poll 返回空快照(0.2 降级),tick 仍 ok。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="",  # OpenRouter 缺 key
                )
                result = await ds.tick()
                assert result.ok is True
                assert result.stats[ScannerSource.OPENROUTER].added == 0
                assert result.stats[ScannerSource.NVIDIA].added == 1
            finally:
                await store.close()
        _run(body())


# ── tick 错误处理 ─────────────────────────────────────────────────

class TestTickErrorHandling:
    def test_fetcher_raising_returns_error_result(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                async def bad_fetch(url, headers, timeout):
                    raise RuntimeError("total net failure")

                # poll_all 内各 poll_* 会 catch 降级空,不抛 → tick ok=True 全 0
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=bad_fetch,
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                # poll_* 内部 catch → 空 snapshot,不冒泡 → ok=True
                assert result.ok is True
                assert result.stats[ScannerSource.NVIDIA].added == 0
            finally:
                await store.close()
        _run(body())


# ── run_loop ──────────────────────────────────────────────────────

class TestRunLoop:
    def test_stop_event_exits_cleanly(self, tmp_path):
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=None,
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                stop = asyncio.Event()

                async def stopper():
                    await asyncio.sleep(0.05)  # 让 tick 跑一轮
                    stop.set()

                await asyncio.gather(ds.run_loop(stop, interval=0.01), stopper())
                # 到这里说明 run_loop 正常退出(没卡死)
                assert stop.is_set()
            finally:
                await store.close()
        _run(body())


# ── build_dynamic_adapters ────────────────────────────────────────

class TestBuildDynamicAdapters:
    def test_builds_candidates_with_keys(self):
        models = [_nv("nvidia/llama-3.1-nemotron-70b-instruct"), _or("openai/gpt-oss-120b:free")]
        candidates = build_dynamic_adapters(
            models, nvidia_key="nv-key", openrouter_key="or-key"
        )
        assert len(candidates) == 2
        names = {c[0] for c in candidates}
        assert "dyn-nvidia-nvidia:llama-3.1-nemotron-70b-instruct" in names
        assert "dyn-openrouter-openai:gpt-oss-120b:free" in names
        # account_key 是 env 名(非 secret)
        keys = {c[2] for c in candidates}
        assert keys == {"NVIDIA_API_KEY", "OPENROUTER_API_KEY"}

    def test_missing_key_source_skipped(self):
        models = [_nv("a"), _or("b:free")]
        candidates = build_dynamic_adapters(
            models, nvidia_key="nv-key", openrouter_key=""  # OR 缺 key
        )
        assert len(candidates) == 1
        assert candidates[0][0].startswith("dyn-nvidia-")

    def test_env_used_when_keys_not_passed(self):
        models = [_nv("a")]
        candidates = build_dynamic_adapters(
            models, env={"NVIDIA_API_KEY": "from-env"}
        )
        assert len(candidates) == 1

    def test_empty_models_returns_empty(self):
        assert build_dynamic_adapters([], nvidia_key="k") == []

    def test_name_stable_and_unique(self):
        """同 model_id 多次造 → 同名(稳定);不同 model → 不同名(唯一)。"""
        models = [_nv("a-70b"), _nv("b-large")]
        cands = build_dynamic_adapters(models, nvidia_key="k")
        names = [c[0] for c in cands]
        assert len(names) == len(set(names))  # 唯一
        cands2 = build_dynamic_adapters(models, nvidia_key="k")
        assert [c[0] for c in cands2] == names  # 稳定


# ── 红线:不接 production Cascade ──────────────────────────────────

def test_dynamic_adapters_not_auto_wired():
    """红线:build_dynamic_adapters 是纯函数产出候选,不修改 app._cascade / 全局状态。"""
    import llm_router.app as app_mod

    before = list(app_mod._cascade._candidate_names)
    models = [_nv("a-70b")]
    build_dynamic_adapters(models, nvidia_key="k")  # 调用
    after = list(app_mod._cascade._candidate_names)
    assert before == after  # production cascade 候选池未被改(routing-change-safety)


# ── B3.1 · on_tick_complete 回调(Phase B 候选池重建触发)───────────────

class TestOnTickComplete:
    def test_callback_fires_when_changes(self, tmp_path):
        """tick 有变更(added>0)→ 调 on_tick_complete,传 TickResult。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                calls = []

                async def on_tick(result):
                    calls.append(result)

                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                    on_tick_complete=on_tick,
                )
                result = await ds.tick()
                assert result.ok is True
                assert len(calls) == 1
                assert calls[0] is result  # 传同一个 TickResult
            finally:
                await store.close()
        _run(body())

    def test_callback_not_fired_when_no_changes(self, tmp_path):
        """tick 无变更(added=0 expired=0)→ 不调 on_tick_complete。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                calls = []

                async def on_tick(result):
                    calls.append(result)

                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                    on_tick_complete=on_tick,
                )
                await ds.tick()       # 首次:added=1 → 调一次
                assert len(calls) == 1
                await ds.tick()       # 二次:同快照 added=0 expired=0 → 不调
                assert len(calls) == 1
            finally:
                await store.close()
        _run(body())

    def test_callback_exception_does_not_crash_tick(self, tmp_path):
        """回调抛异常 → tick 不崩,仍返 TickResult(ok=True)。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                async def bad_on_tick(result):
                    raise RuntimeError("rebuild failed")

                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                    on_tick_complete=bad_on_tick,
                )
                result = await ds.tick()  # 不抛
                assert result.ok is True  # tick 本身成功(回调异常不污染)
                assert result.stats[ScannerSource.NVIDIA].added == 1
            finally:
                await store.close()
        _run(body())

    def test_no_callback_no_error(self, tmp_path):
        """未注入 on_tick_complete(None)→ 正常 tick,无副作用。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                )
                result = await ds.tick()
                assert result.ok is True
            finally:
                await store.close()
        _run(body())

    def test_callback_fires_on_expire_only(self, tmp_path):
        """第二次 tick 模型下架(removed→expired)→ 有变更 → 调回调。"""
        async def body():
            store = ScannerStore(tmp_path / "scanner.db")
            await store.init()
            try:
                calls = []

                async def on_tick(result):
                    calls.append(result)

                ds = DynamicScanner(
                    store,
                    probe_factory=_probe_factory_passing(),
                    fetcher=_fetcher_returning(nv_models=[_nv("a-70b")]),
                    nvidia_key="k",
                    openrouter_key="k",
                    on_tick_complete=on_tick,
                )
                await ds.tick()  # +a-70b
                calls.clear()
                # 模型下架:fetcher 返空
                ds._fetcher = _fetcher_returning(nv_models=[])
                await ds.tick()  # removed a-70b → expired=1 → 调回调
                assert len(calls) == 1
                assert calls[0].stats[ScannerSource.NVIDIA].expired == 1
            finally:
                await store.close()
        _run(body())


# ── B1.1/B1.2 · 动态条目造 ProviderEntry(Phase B)──────────────────────

class TestDynamicEntryToProviderEntry:
    def test_fields_correct_free_zero_cost(self):
        """动态条目 is_free=True/cost_multiplier=0.0,与静态免费 provider 同档竞争。"""
        m = _nv("nvidia/llama-3.1-nemotron-70b-instruct", tier="strong")
        e = dynamic_entry_to_provider_entry(m)
        assert e.is_free is True
        assert e.cost_multiplier == 0.0
        assert e.quota == 500000  # D1 默认(实现时按 source 调)
        assert e.cooldown_s == 30
        assert e.tier == "strong"

    def test_name_matches_build_dynamic_adapters(self):
        """entry name 必须与 build_dynamic_adapters 产出的候选 name 一致
        (EpsilonGreedy._rank 按 name 查 entries;不一致 → missing 报错)。"""
        m = _nv("nvidia/llama-3.1-nemotron-70b-instruct", tier="medium")
        entry = dynamic_entry_to_provider_entry(m)
        cands = build_dynamic_adapters([m], nvidia_key="k")
        assert len(cands) == 1
        assert entry.name == cands[0][0]  # 同 dyn-{source}-{flat_id}

    def test_none_tier_degrades_to_medium(self):
        """None tier(未推断)→ 降级 medium(ProviderEntry.tier Literal 不接受 None)。"""
        m = DiscoveredModel(source=ScannerSource.NVIDIA, model_id="a-70b", tier=None)
        e = dynamic_entry_to_provider_entry(m)
        assert e.tier == "medium"

    def test_name_stable_and_unique_across_models(self):
        m1 = _nv("a-70b", tier="strong")
        m2 = _nv("b-large", tier="fast")
        e1 = dynamic_entry_to_provider_entry(m1)
        e2 = dynamic_entry_to_provider_entry(m2)
        assert e1.name != e2.name
        assert dynamic_entry_to_provider_entry(m1).name == e1.name  # 稳定

    def test_openrouter_source_name(self):
        m = _or("openai/gpt-oss-120b:free", tier="strong")
        e = dynamic_entry_to_provider_entry(m)
        assert e.name == "dyn-openrouter-openai:gpt-oss-120b:free"
        assert e.tier == "strong"

    def test_pure_function_no_global_mutation(self):
        """纯函数:不碰 app._cascade / 全局 entries。"""
        import llm_router.app as app_mod
        before = list(app_mod._cascade._candidate_names)
        dynamic_entry_to_provider_entry(_nv("a-70b"))
        assert list(app_mod._cascade._candidate_names) == before


class TestBuildDynamicEntries:
    def test_batch_builds_entries(self):
        models = [_nv("a-70b", tier="strong"), _or("b:free", tier="fast")]
        entries = build_dynamic_entries(models)
        assert len(entries) == 2
        names = {e.name for e in entries}
        assert "dyn-nvidia-a-70b" in names
        assert "dyn-openrouter-b:free" in names
        # 全部免费零成本
        assert all(e.is_free for e in entries)
        assert all(e.cost_multiplier == 0.0 for e in entries)

    def test_empty_models_returns_empty(self):
        assert build_dynamic_entries([]) == []

    def test_dedup_by_model_id(self):
        """同 source 同 model_id(可能 display_name 不同)→ 去重为一条 entry。"""
        m1 = DiscoveredModel(
            source=ScannerSource.NVIDIA, model_id="a-70b",
            display_name="A", tier="strong",
        )
        m2 = DiscoveredModel(
            source=ScannerSource.NVIDIA, model_id="a-70b",
            display_name="A2", tier="strong",
        )
        entries = build_dynamic_entries([m1, m2])
        assert len(entries) == 1
        assert entries[0].name == "dyn-nvidia-a-70b"

    def test_entries_name_match_adapters(self):
        """build_dynamic_entries 的 name 集合 == build_dynamic_adapters 的 name 集合(对齐)。"""
        models = [_nv("a-70b", tier="strong"), _or("b:free", tier="medium")]
        entry_names = {e.name for e in build_dynamic_entries(models)}
        cand_names = {c[0] for c in build_dynamic_adapters(models, nvidia_key="k", openrouter_key="k")}
        assert entry_names == cand_names

