#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
handoff_ghpages.py -- print the GitHub Pages hand-off and troubleshooting text.

Written in Python on purpose: Windows PowerShell 5.1 decodes a BOM-less .ps1 with
the system ANSI code page, so Chinese literals in a .ps1 become invalid syntax.
All Chinese output lives here, printed as UTF-8.

Usage: python scripts/handoff_ghpages.py [--done|--trouble]
"""
from __future__ import annotations

import argparse
import sys

DONE = """
发布完成时你会看到这段。接下来在 GitHub 网页端做（只需一次）：

  1) 打开仓库设置
       https://github.com/eternallbk/future-platform/settings/pages

  2) Build and deployment
       Source = Deploy from a branch
       Branch = gh-pages   /   (root)
       Save

  3) 等 1~2 分钟，访问
       https://eternallbk.github.io/future-platform/
     手机访问同一个地址即可。

  之后每次更新站点：
       python scripts\\build_site.py
       powershell -ExecutionPolicy Bypass -File scripts\\publish-gh-pages.ps1
    每日 20:00 的定时任务已包含这两步，无需手动执行。
"""

TROUBLE = """
推送失败的常见原因与处理：

  A) 网络到不了 GitHub（本次实测就是这个原因）
     Test-NetConnection github.com -Port 443 返回 False，git 报
     "Failed to connect to github.com port 443"。这是本机网络问题，与脚本或
     凭据无关。换网络或代理后重试：
       powershell -ExecutionPolicy Bypass -File scripts\\publish-gh-pages.ps1
     注意：本地采集不受影响，只是网站内容会停在上一次成功发布的版本。

  B) 没有可用的凭据
     本机 system 级 credential.helper = manager，且已存有 github.com 凭据，
     实测可非交互推送。若令牌过期，重新生成细粒度 PAT：
       1. https://github.com/settings/personal-access-tokens/new
       2. Fine-grained token
          Repository access = Only select repositories -> future-platform
          Permissions -> Repository permissions -> Contents = Read and write
          有效期按需（例如 90 天，过期后重新生成）
       3. 推送时把令牌当作密码填入，Git Credential Manager 会保存到
          Windows 凭据管理器（定时任务也能用，且不落明文）。

  C) 已经是最新内容
     脚本按 tree 哈希 判断远端是否已与本次构建一致：一致时打印
     "gh-pages already matches this build exactly"，这是成功而不是失败。

  安全说明：推送前会再做一次隐私审计（state/、inbox/、harness 日志、
  sk- 形状的凭据字符串）。审计不通过就中止，且不推送任何内容。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--done", action="store_true")
    ap.add_argument("--trouble", action="store_true")
    args = ap.parse_args()
    print(TROUBLE if args.trouble else DONE)
    return 0


if __name__ == "__main__":
    sys.exit(main())