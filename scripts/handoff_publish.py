#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
handoff_publish.py — 打印 GitHub Pages 的发布步骤（中文）

为什么单独一个 Python 文件：Windows PowerShell 5.1 用系统 ANSI 代码页解码
无 BOM 的 .ps1，所以 .ps1 里不能出现中文（会被解成非法语法）。所有中文输出
统一由这个脚本负责，它用 UTF-8 正常打印。

用法: python scripts/handoff_publish.py [--branch gh-pages]
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="gh-pages")
    args = ap.parse_args()

    b = args.branch
    print(f"""
需要在 GitHub 网页端做一次（只能你自己做，我无法代你登录）：

  1) 新建仓库  https://github.com/new
     · 名称随意，例如 future-workbench
     · 可见性必须 Public（GitHub Pages 免费版只支持公开仓库）
     · 不要勾选 Add a README / Add .gitignore（避免首次推送冲突）

  2) 关联并推送源码（把 <你的用户名> 和 <仓库名> 换成实际值）
       git remote add origin https://github.com/<你的用户名>/<仓库名>.git
       git branch -M main
       git push -u origin main

  3) 部署站点（推荐：产物单独放 {b} 分支，源码与产物分离）
       git subtree push --prefix dist origin {b}
     · 以后每次更新站点：先 python scripts\\build_site.py，再重复上面这一条

     备选（更省事，但会把产物提交进主分支）
       Copy-Item -Recurse -Force dist\\* docs\\site\\
       git add docs/site; git commit -m "publish site"; git push
       然后在步骤 4 里把目录选成 /docs/site

  4) 打开 Pages 设置
       仓库 → Settings → Pages
       Source = Deploy from a branch
       Branch = {b}   （或 main + /docs/site）
       Save

  5) 等 1~2 分钟，访问
       https://<你的用户名>.github.io/<仓库名>/
     手机访问同一个地址即可。

注意事项
  · GitHub Pages 不读取 _headers，所以 CSP 等响应头不会生效。
    如果你更看重安全响应头，改用 Cloudflare Pages（拖拽 dist/ 即可）。
  · 仓库是公开的：web/data/state/、web/data/inbox/、research/_probe/ 已在
    .gitignore 中排除。如果你以后往 web/data/ 加新的数据文件，记得确认它
    是否应该公开 —— build_site.py 用的是白名单，默认不会发布新文件。
  · 站点是静态快照：手机看到的内容 = 最后一次 build_site.py 的数据。
    想每天自动更新，就在 run-daily.ps1 收尾加一行
      & $Python (Join-Path $ScriptDir 'build_site.py')
    再让 GitHub Pages 从仓库自动部署（push 后自动生效）。
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
