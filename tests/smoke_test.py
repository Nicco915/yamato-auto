#!/usr/bin/env python3
"""冒烟测试：以子进程方式驱动 scripts/run_cli.py 全流程并校验关键输出。

直接运行：python3 tests/smoke_test.py

血泪红线（2026-08-11）：子进程 import 链（llm_client.py）会执行
load_dotenv(override=True)，把父进程预设的隔离环境变量（CHECKPOINT_DB_PATH
等）**覆盖回 .env 真实路径**——本测试的 --reset 曾因此删除生产
checkpoints.db / master.db。进程内隔离对子进程无效，必须让子进程加载
一份"临时 .env"（db / output / sessions 全部指向临时目录）。
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def _make_isolated_env(tmp: Path) -> dict[str, str]:
    """基于真实 .env 生成临时 .env（db/output 替换为临时目录），返回子进程 env。

    保留 UPSTREAM_ROOT / DOWNSTREAM_FILE_PATH 真实路径（run_cli 需要解析
    真实下游文件，只读不写原件）；db / output / sessions 全部隔离。
    """
    tmp_db_dir = tmp / "db"
    lines: list[str] = []
    replaced = set()
    for line in (APP_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        for key, tmp_path in (
            ("MASTER_DB_PATH", tmp_db_dir / "master.db"),
            ("CHECKPOINT_DB_PATH", tmp_db_dir / "checkpoints.db"),
            ("OUTPUT_DIR", tmp / "output"),
        ):
            if line.startswith(f"{key}="):
                line = f"{key}={tmp_path}"
                replaced.add(key)
                break
        lines.append(line)
    # .env 里原本没有的 key（如 OUTPUT_DIR 走默认值时）补写到末尾
    if "OUTPUT_DIR" not in replaced:
        lines.append(f"OUTPUT_DIR={tmp / 'output'}")
    tmp_dotenv = tmp / ".env"
    tmp_dotenv.write_text("\n".join(lines) + "\n", encoding="utf-8")

    env = dict(os.environ)
    # 子进程 llm_client 优先加载这份临时 .env（第一道防线）
    env["YAMATO_DOTENV_PATH"] = str(tmp_dotenv)
    # sessions 目录不读 .env，走独立 env 变量（session.py 模块级读取）
    env["YAMATO_SESSIONS_DIR"] = str(tmp / "sessions")
    # 兜底：即便临时 .env 漏配某 key，预设 env 也能接住
    # （override=True 只覆盖 .env 里存在的 key）
    env["CHECKPOINT_DB_PATH"] = str(tmp_db_dir / "checkpoints.db")
    env["MASTER_DB_PATH"] = str(tmp_db_dir / "master.db")
    env["OUTPUT_DIR"] = str(tmp / "output")
    return env


def test_cli_end_to_end():
    tmp = Path(tempfile.mkdtemp(prefix="yamato_smoke_test_"))
    result = subprocess.run(
        [sys.executable, "-u", "scripts/run_cli.py", "--reset"],
        cwd=APP_ROOT,
        env=_make_isolated_env(tmp),
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
