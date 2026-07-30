#!/usr/bin/env python3
"""密钥安全审计脚本：扫描git和配置文件中的明文密钥。"""
import os
import re
import subprocess
from pathlib import Path


def scan_git_history():
    """扫描git历史中的明文密钥。"""
    print("=== 扫描git历史中的明文密钥 ===")

    # 获取git目录
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2]
    )

    if result.returncode != 0:
        print("⚠️  不是git仓库，跳过历史扫描")
        return []

    git_dir = Path(result.stdout.strip())
    if not git_dir.exists():
        print("⚠️  .git目录不存在，跳过历史扫描")
        return []

    # 常见密钥模式
    patterns = [
        r"sk-[a-zA-Z0-9]{32,}",  # OpenAI API key
        r"AKIA[0-9A-Z]{16}",  # AWS access key
        r"ghp_[a-zA-Z0-9]{36,}",  # GitHub personal access token
        r"xoxb-[0-9]{10,13}\\.[0-9]{10,13}\\.[0-9a-zA-Z]{24}",  # Slack bot token
        r"[a-zA-Z0-9]{32,}=.+",  # Base64编码的密钥
    ]

    findings = []

    # 扫描最近100次提交
    try:
        commits = subprocess.run(
            ["git", "log", "--oneline", "-100"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.splitlines()

        for commit in commits:
            commit_hash = commit.split()[0]

            # 获取提交的文件内容
            files = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
                capture_output=True,
                text=True,
                check=True
            ).stdout.splitlines()

            for file_path in files:
                if file_path.endswith((".md", ".txt", ".env")):
                    continue  # 跳过文档和环境变量文件

                try:
                    content = subprocess.run(
                        ["git", "show", f"{commit_hash}:{file_path}"],
                        capture_output=True,
                        text=True,
                        check=True
                    ).stdout

                    for pattern in patterns:
                        if re.search(pattern, content):
                            findings.append({
                                "commit": commit_hash,
                                "file": file_path,
                                "pattern": pattern
                            })
                            break
                except subprocess.CalledProcessError:
                    continue

    except subprocess.CalledProcessError as e:
        print(f"⚠️  扫描git历史失败: {e}")

    return findings


def scan_current_files():
    """扫描当前文件中的明文密钥。"""
    print("\n=== 扫描当前文件中的明文密钥 ===")

    findings = []

    # 扫描目标文件
    target_files = [
        "router-policy.yaml",
        ".env",
        "docker-compose.yml",
        "Dockerfile",
    ]

    # 密钥模式
    patterns = [
        (r"api_key:\s*['\"]?[a-zA-Z0-9]{20,}['\"]?", "api_key字段"),
        (r"password:\s*['\"]?[a-zA-Z0-9]{8,}['\"]?", "password字段"),
        (r"secret:\s*['\"]?[a-zA-Z0-9]{16,}['\"]?", "secret字段"),
    ]

    project_root = Path(__file__).resolve().parents[2]

    for file_name in target_files:
        file_path = project_root / file_name
        if not file_path.exists():
            continue

        try:
            content = file_path.read_text()

            for pattern, description in patterns:
                if re.search(pattern, content):
                    findings.append({
                        "file": file_name,
                        "pattern": description
                    })
        except Exception as e:
            print(f"⚠️  读取{file_name}失败: {e}")

    return findings


def scan_env_variables():
    """检查环境变量中的明文密钥。"""
    print("\n=== 检查环境变量密钥 ===")

    sensitive_vars = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ADMIN_SECRET_KEY",
        "SECRET_ENCRYPTION_KEY",
    ]

    findings = []

    for var in sensitive_vars:
        value = os.environ.get(var)
        if value:
            # 检查是否是明显的测试密钥
            if value in ["dev-secret-key", "test-key", "sk-test"]:
                print(f"ℹ️  {var}: 测试密钥（开发环境正常）")
            else:
                findings.append({
                    "variable": var,
                    "value": value[:8] + "..." if len(value) > 8 else "***"
                })

    return findings


def main():
    """主函数：执行所有审计扫描。"""
    print("🔍 开始密钥安全审计...\n")

    # 扫描git历史
    git_findings = scan_git_history()

    # 扫描当前文件
    file_findings = scan_current_files()

    # 检查环境变量
    env_findings = scan_env_variables()

    # 汇总结果
    print("\n=== 审计结果汇总 ===")
    all_findings = len(git_findings) + len(file_findings) + len(env_findings)

    if all_findings == 0:
        print("✅ 未发现明文密钥泄露风险")
        return 0
    else:
        print(f"⚠️  发现 {all_findings} 个潜在风险:")

        if git_findings:
            print(f"\n📌 Git历史 ({len(git_findings)}):")
            for finding in git_findings:
                print(f"  - 提交 {finding['commit'][:8]} 文件 {finding['file']}")

        if file_findings:
            print(f"\n📌 当前文件 ({len(file_findings)}):")
            for finding in file_findings:
                print(f"  - {finding['file']}: {finding['pattern']}")

        if env_findings:
            print(f"\n📌 环境变量 ({len(env_findings)}):")
            for finding in env_findings:
                print(f"  - {finding['variable']}: {finding['value']}")

        print("\n建议:")
        print("1. 立即移除git历史中的密钥（git filter-branch或BFG Repo-Cleaner）")
        print("2. 将敏感配置移至环境变量或SecretStore")
        print("3. 轮换已泄露的密钥")
        print("4. 添加.env.gitignore到.gitignore")

        return 1


if __name__ == "__main__":
    exit(main())
