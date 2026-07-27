#!/usr/bin/env python3
"""冒烟测试：以子进程方式驱动 scripts/run_cli.py 全流程并校验关键输出。

直接运行：python3 tests/smoke_test.py
"""
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def test_cli_end_to_end():
    result = subprocess.run(
        [sys.executable, "-u", "scripts/run_cli.py", "--reset"],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = result.stdout + result.stderr
    print(out)
    assert result.returncode == 0, f"CLI 冒烟失败，退出码 {result.returncode}"
    # 关键断言
    assert "pending_human_review" in out or "挂起" in out
    assert "断言通过" in out
    assert "冒烟测试全部通过" in out


if __name__ == "__main__":
    test_cli_end_to_end()
    print("smoke_test: PASS")
