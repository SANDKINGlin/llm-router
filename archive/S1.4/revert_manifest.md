# 回滚清单 — S1.4(revert 模板骨架)

## 改动清单
**新增文件**(回滚 = 删除):
- scripts/revert.sh(本切片把桩变真模板)
- archive/TEMPLATE_revert_manifest.md
- tests/unit/test_revert_template.py

**修改文件**:无

## 还原步骤(代码)
1. git revert <S1.4 的 commit-sha>(baseline 之后)
2. 或手动删上述新增文件

## 副作用清理(脚本自动)
- DB: data/*.db*(本切片无 DB 改动,清理无影响)
- 进程: :8789(本切片不起服务)

## 验证回滚
- pytest 回到 53p(S1.6 终态)
