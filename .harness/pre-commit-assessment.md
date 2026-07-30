# llm-router 轻量提交门评估

结论：当前不安装 pre-commit，也不增加 Git hook。

真实依据：

1. 仓库 `pyproject.toml` 只声明 pytest 等 dev 依赖，没有 ruff/black/mypy/pre-commit。
2. 当前 fast 单测真实结果为 FAIL（8 failed），medium 集成测试因缺 cryptography 在 collection 阶段 FAIL，只有 slow E2E 10/10 PASS。
3. 将当前失败测试接入 pre-commit 会阻塞所有提交；绕过 hook 又使门禁失去价值。
4. 为了启用 pre-commit 而安装新依赖或顺手修项目代码，会越过本切片边界并污染当前已有未提交工作树。
5. 现阶段 `.harness/runner.py` 已提供可审计的只读验证入口，且诚实记录失败，比仓促引入可绕过的本地 hook 更安全。

触发重新评估的硬条件：

- fast profile 恢复稳定 PASS；
- 仓库明确选定 lint/format 工具并写入 pyproject；
- Hook 目标耗时有实测且适合提交前反馈；
- 依赖安装在项目 `.venv`，不污染 Hermes venv；
- 三方共同验证 hook 不修改业务代码、不吞失败。

当前裁决：NOT_APPLICABLE（暂不实施），不是遗漏。
