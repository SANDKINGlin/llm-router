# 回滚清单模板 — `<slice-id>`

> 每个切片完成后,复制本文件到 `archive/<slice-id>/revert_manifest.md` 并填写。
> `scripts/revert.sh --dry-run <slice-id>` 会读本清单 + 打印将清理的副作用;
> `--exec <slice-id> --yes` 会真执行(DB/进程清理 + 提示按本清单还原代码)。

## 改动清单

**新增文件**(回滚 = 删除):
- `path/to/new-file`

**修改文件**(回滚 = 还原到切片前版本):
- `path/to/modified-file` — 还原方式:`git checkout <ref> -- <file>` / 手动

**删除文件**(回滚 = 恢复):
- (无)

## 还原步骤(代码)

1. (按上面清单逐项操作。当前项目 git master 无 commit → 手动还原;
   纳入版本控制后改用 `git revert/reset`。)
2. ...

## 副作用清理(脚本自动,见 revert.sh)

- DB 文件:`data/*.db{,-wal,-shm}`(trace/ledger/task/circuit/health 五库 + WAL 旁路)
- 进程:`:8789` 路由服务(fuser -k;**勿用 pkill -f**,模式串自匹配会自杀 exit 144)

## 验证回滚成功

- [ ] `PYTHONPATH=src .venv/bin/python -m pytest tests -q` 全绿(回归到切片前基线)
- [ ] `curl -s http://127.0.0.1:8789/healthz` 返 200(若该切片不涉及 readiness 则免)

## 备注

- design.md D9:回滚挂 `routing-change-safety` 协议(逐步切 + 独立验证 + 可回滚)。
- 若回滚涉及切 agent 走路由层,须同步回滚 ANTHROPIC_BASE_URL(见 memory `routing-change-safety`)。
