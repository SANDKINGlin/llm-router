#!/usr/bin/env bash
# S1.4 · 切片级回滚操作模板骨架。
#
# 当某个切片(S1.x/S2.x...)做完后发现要撤掉,用本脚本回滚该切片的改动 + 清理运行时副作用。
#
# 用法:
#   scripts/revert.sh                          # 打印 usage
#   scripts/revert.sh --check                  # 自检(语法 + 依赖 fuser/lsof)
#   scripts/revert.sh --dry-run <slice>        # 打印该切片回滚清单,不执行(默认安全)
#   scripts/revert.sh --exec <slice> --yes     # 执行回滚(须 --yes 二次确认)
#
# 设计:
#   - 基于 archive/<slice>/revert_manifest.md(每切片完成后复制模板填写改动清单 + 还原步骤)。
#   - 默认 dry-run;--exec 必须带 --yes(回滚是破坏性操作,防误触)。
#   - 自动清理的副作用:data/*.db* (5 独立 SQLite WAL + 旁路 -wal/-shm 文件) +
#     :8789 路由进程(用 fuser -k,非 pkill — 后者会自杀 exit 144)。
#   - 代码还原:当前项目 git master 无 commit(git revert 用不了),按 manifest 手动还原;
#     待项目纳入版本控制后,本脚本可增强为 `git revert/reset <ref>`(留 TODO 标记)。
#   - 与 design.md D9 一致:回滚挂 routing-change-safety + service-startup-checklist。
set -euo pipefail

PROJ_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PROJ_ROOT/data"
ARCHIVE_DIR="$PROJ_ROOT/archive"
PORT=8789

usage() {
    cat <<'EOF'
revert.sh — 切片级回滚模板(S1.4)

  scripts/revert.sh --check                自检
  scripts/revert.sh --dry-run <slice>      打印回滚清单(不执行)
  scripts/revert.sh --exec <slice> --yes   执行回滚(须二次确认)

<slice> 如 S1.6;对应清单:archive/<slice>/revert_manifest.md
EOF
}

# 杀 :PORT 进程(fuser 优先,lsof 兜底;绝不用 pkill -f —— 模式串自匹配会杀自己)
kill_port() {
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${PORT}/tcp" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids="$(lsof -ti "tcp:${PORT}" 2>/dev/null || true)"
        [[ -n "$pids" ]] && kill $pids 2>/dev/null || true
    else
        echo "warn: 既无 fuser 也无 lsof,跳过杀 :${PORT}" >&2
    fi
}

cmd_check() {
    bash -n "$0"   # 语法自检
    local missing=0
    for dep in fuser lsof; do
        if ! command -v "$dep" >/dev/null 2>&1; then
            echo "warn: 缺 $dep(杀进程会降级或跳过)" >&2
        fi
    done
    [[ -d "$ARCHIVE_DIR" ]] || echo "warn: 无 $ARCHIVE_DIR(还没切片归档过)" >&2
    echo "revert.sh 自检通过"
}

_manifest_path() { echo "$ARCHIVE_DIR/$1/revert_manifest.md"; }

cmd_dry_run() {
    local slice="$1"
    local manifest
    manifest="$(_manifest_path "$slice")"
    if [[ ! -f "$manifest" ]]; then
        echo "错误:无回滚清单 $manifest" >&2
        echo "提示:复制 $ARCHIVE_DIR/TEMPLATE_revert_manifest.md 到该路径并填写" >&2
        exit 2
    fi
    echo "=== 回滚清单 [$slice] ==="
    cat "$manifest"
    echo
    echo "=== 脚本将自动清理的副作用(dry-run,未执行)==="
    echo "  DB 文件: $DATA_DIR/*.db{,-wal,-shm}"
    echo "  进程   : :$PORT (fuser -k)"
    echo "  代码   : 按 manifest 手动/git(项目暂无 commit,git revert 不可用)"
    echo
    echo "确认无误后:scripts/revert.sh --exec $slice --yes"
}

cmd_exec() {
    local slice="$1"
    local confirmed="${2:-}"
    if [[ "$confirmed" != "--yes" ]]; then
        echo "拒绝执行:--exec 必须带 --yes 二次确认(回滚是破坏性操作)" >&2
        exit 3
    fi
    cmd_dry_run "$slice" >/dev/null   # 顺带校验 manifest 存在
    echo ">>> 执行回滚 [$slice] ..."
    # 1) 清理 DB 副作用(5 独立 SQLite WAL + 旁路文件)
    if [[ -d "$DATA_DIR" ]]; then
        rm -f "$DATA_DIR"/*.db "$DATA_DIR"/*.db-wal "$DATA_DIR"/*.db-shm 2>/dev/null || true
        echo "    清理 DB: $DATA_DIR/*.db*"
    fi
    # 2) 杀路由进程
    kill_port
    echo "    杀进程: :$PORT"
    # 3) 代码还原:提示按 manifest 手动(无 git commit)
    echo ">>> 代码还原:请按 manifest 中的『还原步骤』手动操作"
    echo "    (项目 git master 暂无 commit;纳入版本控制后可改 git revert/reset)"
    echo ">>> 回滚完成。验证:pytest 全绿 + curl :$PORT/healthz"
}

main() {
    case "${1:-}" in
        --check)    cmd_check ;;
        --dry-run)  [[ $# -ge 2 ]] || { usage; exit 1; }; cmd_dry_run "$2" ;;
        --exec)     [[ $# -ge 2 ]] || { usage; exit 1; }; cmd_exec "$2" "${3:-}" ;;
        -h|--help|"") usage ;;
        *) echo "未知参数: $1" >&2; usage; exit 1 ;;
    esac
}

main "$@"
