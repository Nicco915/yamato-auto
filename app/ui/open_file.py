# -*- coding: utf-8 -*-
"""跨平台"用系统默认程序打开本地文件"模块。

按 CLAUDE.md 硬性要求，本项目必须同时兼容 macOS 与 Windows。本模块把
"用默认程序打开文件"这件事独立成单独模块，便于单元测试，也便于将来
增加新平台（如 WSL / ChromeOS）时集中改动。

平台分派：
- Windows（os.name == "nt"）：os.startfile(path)，非阻塞；系统会异步启动
  与文件后缀关联的默认程序（如 .xlsx 启动 Excel）。os.startfile 是
  Windows-only，跨平台调用必须先判断 os.name，否则在 macOS/Linux 上会
  AttributeError。
- macOS（sys.platform == "darwin"）：subprocess.run(["open", str(path)])，
  check=True 失败抛 CalledProcessError，timeout=10 防止对方程序挂死
  把我们的请求线程也拖住。
- Linux（兜底）：同上但用 xdg-open；不同发行版包名可能不同（xdg-utils），
  缺失时抛 FileNotFoundError。
- 其他平台（理论上不应触发）：抛 OpenFileError（中文提示）。

异常统一包装：
- FileNotFoundError（xdg-open / open 不存在，命令找不到）
- subprocess.CalledProcessError（命令返回非零）
- subprocess.TimeoutExpired（10 秒内未启动）
全部包成自定义 OpenFileError；原始异常通过 __cause__ 串联保留，方便
排障。错误消息用中文，因为会透传给浏览器端的最终用户看。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class OpenFileError(Exception):
    """用默认程序打开文件失败。

    统一包装三大类底层异常：
    - FileNotFoundError：系统命令（open / xdg-open）不存在
    - subprocess.CalledProcessError：命令执行但返回非零
    - subprocess.TimeoutExpired：10 秒内未启动默认程序

    message 直接透传给前端用户，故用中文。
    """

    pass


def open_with_default_app(path: Path) -> None:
    """用系统默认程序打开本地文件（异步启动，不等待默认程序退出）。

    参数
    ----
    path : Path
        待打开文件的绝对路径。调用方需自行保证文件存在、是文件、后缀合法。
        本函数不做文件存在性检查（那是上层 router 的事），只关心"把启动
        这件事做完"。

    异常
    ----
    OpenFileError
        命令不存在 / 执行失败 / 超时 / 不支持的平台。

    平台差异
    --------
    - Windows: os.startfile 非阻塞，立即返回；Excel 进程在后台启动
    - macOS / Linux: subprocess.run + timeout=10；命令返回即视为成功
      （默认程序在后台运行，本函数不会等它退出）
    """
    # Windows: os.startfile 是非阻塞调用，立即返回
    if os.name == "nt":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
        except OSError as e:
            # os.startfile 自身抛的 OSError（如文件不存在、关联程序缺失）
            raise OpenFileError(
                f"Windows 系统启动默认程序失败: {e}"
            ) from e
        return

    # macOS / Linux: 走 subprocess，超时 10s
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
        err_label = "macOS open 命令"
    elif sys.platform.startswith("linux"):
        # Linux 兜底（项目本身不在 Linux 上生产部署，但 CI / 测试环境可能用到）
        cmd = ["xdg-open", str(path)]
        err_label = "xdg-open 命令"
    else:
        # 其他平台（如 FreeBSD / 其他 Unix）：本项目不承诺支持
        raise OpenFileError(
            f"不支持的操作系统平台: sys.platform={sys.platform}, os.name={os.name}"
        )

    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=10,
            capture_output=True,
        )
    except FileNotFoundError as e:
        # 命令不存在（Linux 上 xdg-utils 未装最常见）
        raise OpenFileError(
            f"{err_label}不存在，无法打开文件: {e}"
        ) from e
    except subprocess.CalledProcessError as e:
        # 命令执行但返回非零
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        raise OpenFileError(
            f"{err_label}执行失败: {stderr or '未知错误'}"
        ) from e
    except subprocess.TimeoutExpired as e:
        # 10 秒内未启动（默认程序可能卡在权限弹窗）
        raise OpenFileError(
            f"{err_label}在 10 秒内未启动默认程序"
        ) from e