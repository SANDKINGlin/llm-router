#!/usr/bin/env bash
# P8 · 供应链依赖锁——compile + hash + dry-run 验证 + 原子落盘(单事务)。
#
# 修 BUG-piphash-01(两步非原子:先 compile 再单独 pip hash,中间窗错配)。
# 修 D-1(审查 06-14 发现):uv --require-hashes 只在【实际下载/构建】时验 hash。
#   热 dev venv(--apply 无下载)与 dry-run(--verify)都不验 → 给假象"已审计"。
#   故 --audit 用 fresh temp venv + --no-cache 强制全包真下载,才真触发 hash 校验。
#
# 三种模式:
#   scripts/hash_lock.sh          # 锁:compile+hash → dry-run 验证(零污染)→ 原子 mv
#   scripts/hash_lock.sh --apply  # 同步:真实 install ← 锁(落 dev venv,幂等)。
#                                  #   ⚠ 已装满则无下载=不验 hash;真审计用 --audit
#   scripts/hash_lock.sh --verify # drift:dry-run ← 锁(不改 venv/锁)。
#                                  #   ⚠ dry-run 不下载=不验 hash;真审计用 --audit
#   scripts/hash_lock.sh --audit  # ★真供应链审计:fresh temp venv + --no-cache 全包真下载
#                                  #   + --require-hashes 全 hash 校验。不污染 dev venv。慢(网络)。
#
# uv 原生:`uv pip compile`(= pip-compile)+ `uv pip install --require-hashes`。
# 重跑幂等 = 自动重锁:改 requirements.in 后再跑即刷新全锁。

set -euo pipefail

PROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROOT"

VENV_PY="${VENV_PY:-$PROOT/.venv/bin/python}"
LOCK="$PROOT/requirements.txt"
IN="$PROOT/requirements.in"

# 公共:dry-run 验证(零 venv 副作用,只验解析/drift,【不验 hash】)
_dryrun_verify() {
    uv pip install --python "$VENV_PY" --require-hashes --dry-run -r "$1"
}

case "${1:-lock}" in
  --verify)
    [ -f "$LOCK" ] || { echo "✗ 缺 $LOCK——先跑 hash_lock.sh 生成" >&2; exit 1; }
    echo "[verify] dry-run drift ← $LOCK"
    _dryrun_verify "$LOCK" >/dev/null
    echo "✓ drift 过(未改 venv/锁)。⚠ 未做 hash 审计(dry-run 不下载);真审计: scripts/hash_lock.sh --audit"
    ;;

  --apply)
    [ -f "$LOCK" ] || { echo "✗ 缺 $LOCK" >&2; exit 1; }
    echo "[apply] uv pip install --require-hashes ← $LOCK(落 dev venv,幂等)"
    uv pip install --python "$VENV_PY" --require-hashes -r "$LOCK"
    echo "✓ dev venv 同步 ≡ $LOCK。⚠ 已装满则本次无下载=未验 hash;真审计: scripts/hash_lock.sh --audit"
    ;;

  --audit)
    # ★ 真供应链审计:fresh temp venv + --no-cache → 全包真下载 → --require-hashes 逐包校验。
    # 不碰 dev venv。篡改任意 hash → uv "Hash mismatch" exit≠0(已实测)。
    # --no-cache 禁整个 uv 缓存(wheel+source),实测篡改 hash 必被拦(annotated-types)。
    # ⚠ 已知固有限制:pip/uv hash 验的是【分发包 archive】(wheel/sdist 源码)hash;
    #   sdist 经 setup.py 构建出的产物 hash 不验(恶意构建脚本不在 hash 保护内)。
    #   这是 pip/uv 通用局限,非本脚本独有;深度防护需可复现构建,超出 P8 范围。
    [ -f "$LOCK" ] || { echo "✗ 缺 $LOCK" >&2; exit 1; }
    AUDIT_DIR="$(mktemp -d)"
    trap 'rm -rf "${AUDIT_DIR:-}"' EXIT          # :- 守 set -u;SIGKILL 残留由 /tmp tmpfs 兜底
    echo "[audit] fresh temp venv + --no-cache 全包真下载 + hash 校验 ← $LOCK"
    uv venv "$AUDIT_DIR/venv" >/dev/null
    uv pip install --python "$AUDIT_DIR/venv/bin/python" \
        --require-hashes --no-cache -r "$LOCK" >/dev/null
    n_pkg=$(grep -cE '^[A-Za-z0-9][^#]*==' "$LOCK")
    echo "✓ 供应链审计过:$n_pkg 包 fresh 下载 + 全 hash 校验通过(dev venv 未污染)"
    ;;

  lock|"")
    [ -f "$IN" ] || { echo "✗ 缺 $IN" >&2; exit 1; }
    TMP="$LOCK.tmp.$$"
    trap 'rm -f "$TMP"' EXIT

    echo "[1/3] compile + --generate-hashes → $TMP"
    uv pip compile "$IN" -o "$TMP" --generate-hashes
    sed -i "s|$TMP|$LOCK|g" "$TMP"            # 头部归一(去 tmp PID 名)

    echo "[2/3] dry-run 验证 ← $TMP(零 venv 副作用)"
    _dryrun_verify "$TMP" >/dev/null

    echo "[3/3] 原子落盘: mv $TMP → $LOCK"
    mv -f "$TMP" "$LOCK"
    trap - EXIT

    n_pkg=$(grep -cE '^[A-Za-z0-9][^#]*==' "$LOCK")
    echo "✓ P8 锁定: $LOCK($n_pkg 包)。"
    echo "  同步 dev venv: scripts/hash_lock.sh --apply  |  真供应链审计: scripts/hash_lock.sh --audit"
    ;;

  *)
    echo "用法: $0 [--apply|--verify|--audit]" >&2
    echo "  (无参)   重锁:compile+hash+dry-run验证+原子落盘" >&2
    echo "  --apply  同步 dev venv(幂等;不验 hash)" >&2
    echo "  --verify drift dry-run(不验 hash)" >&2
    echo "  --audit  ★真供应链 hash 审计(fresh temp venv 全包校验)" >&2
    exit 2
    ;;
esac
