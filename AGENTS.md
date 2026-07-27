# AGENTS.md — llm-router 项目 Harness

## 项目身份

- 项目：llm-router
- 类型：Python 3.11+ / FastAPI / CLI
- SDD 主格式：OpenSpec（`openspec/` 已真实采用；不得并行迁移 Spec Kit）
- 本文件只登记仓库级真实边界和命令；全局安全规则继承 `/home/lin/AGENTS.md`。

## 并行与所有权

- 并行 writer 必须使用独立 worktree/branch；Hermes 使用 `hermes -w`。
- 当前仓库已有未提交用户改动。任何任务必须建立 `.agent/task-manifest.yaml` 并限定 allowed_paths；不得清理、覆盖或提交任务范围外改动。
- `router-policy.yaml`、`mnfst/providers.yaml`、公共 API/schema/config 默认 shared/read-only，只有 task manifest 的 contract_owner 可修改。

## 已验证项目入口

- Python：`.venv/bin/python`
- 配置来源：`pyproject.toml`
- 测试发现：`[tool.pytest.ini_options]` 指向 `tests`，pythonpath 为 `src`
- setup：`.venv/bin/python -m pip install -e '.[dev]'`（只在明确授权且隔离 venv 时运行；Harness 不自动安装）
- fast：`.venv/bin/python -m pytest tests/unit -q`
- medium：`.venv/bin/python -m pytest tests/unit tests/integration -q`
- slow：`.venv/bin/python -m pytest tests/e2e -q`
- full：`.venv/bin/python -m pytest tests -q`
- lint：NOT_APPLICABLE（`pyproject.toml` 未声明 lint 工具）
- typecheck：NOT_APPLICABLE（`pyproject.toml` 未声明 typecheck 工具）
- architecture_checks：NOT_APPLICABLE（尚未试点依赖检查器）

机器入口：`.harness/manifest.yaml`。退出码非 0 为 FAIL；不得通过修改测试预期掩盖真实缺陷。

## 证据与交付

- 证据写入 `.harness/evidence/` 或任务 manifest 指定的临时路径。
- 必须记录 changed_paths、command、exit_code、duration_ms、artifact 和三方报告。
- 只有 CC、Codex、Hermes 执行后验证全部通过，任务才可交付。
- 现有未提交改动不是本 Harness 试点的产物，必须保持不变。
