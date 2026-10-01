# Future · 求职学习工作台

面向 **多模态算法 / Post-training 后训练 / 生成式模型 / 世界模型** 方向的算法实习求职工作台。
每天上海时间 **20:00** 自动联网调研，把散落在 arXiv、HuggingFace、GitHub、牛客、各厂招聘官网的
信息整理成可执行的知识卡片、公式剖析、题库与岗位看板。

```
┌──────────────────────────── 每天 20:00 (Asia/Shanghai) ───────────────────────────┐
│ Windows 任务计划程序 → scripts/run-daily.ps1                                      │
│                                                                                   │
│  第 1 层（确定性，必须成功）                                                       │
│    scripts/collect.py   24 个公开渠道并发抓取 → 规范化 → 四层去重 → 相关度打分     │
│                         → 原子写入 web/data/                                      │
│    scripts/selfcheck.py 完整性 / 真实性 / 一致性闸门（有错就报警）                  │
│                                                                                   │
│  第 2 层（可选，失败不影响第 1 层）                                                │
│    scripts/daily-agent.md + dsh headless                                          │
│                         → 深读高相关条目、生成公式剖析与易读化解释、写提案          │
└───────────────────────────────────────────────────────────────────────────────────┘
                                    ↓
                         web/data/*.json  ← 纯数据，无服务端
                                    ↓
                    web/index.html  ← 零依赖前端，双击即可打开
```

---

## 一、30 秒上手

**打开工作台**（两种方式，任选）：

```powershell
# 方式 A：直接双击（file:// 即可运行，不需要服务器）
start web\index.html

# 方式 B：本地静态服务器（推荐，数据刷新更顺）
python -m http.server 8787 --directory web
start http://127.0.0.1:8787
```

**手动跑一次每日调研**：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run-daily.ps1
# 只看确定性采集层（不调用 Agent，快）
powershell -ExecutionPolicy Bypass -File scripts\run-daily.ps1 -SkipAgent
# 查看当前状态
powershell -ExecutionPolicy Bypass -File scripts\run-daily.ps1 -Status
```

**定时任务**（已注册，每天 20:00 自动执行）：

```powershell
schtasks /Query /TN "Future-Workbench-Daily-20" /FO LIST /V   # 查看
schtasks /Run   /TN "Future-Workbench-Daily-20"               # 立即触发一次
schtasks /Delete /TN "Future-Workbench-Daily-20" /F           # 删除
```

---

## 二、工作台的更新周期（重要）

**只有任务计划程序在每天 20:00 (Asia/Shanghai)触发的那一次才是「正常更新」。**
其余写入都是人为触发的调试运行，不应被视为"定时任务在乱跑"。

怎么自己确认：

```powershell
# 看任务本身：只应有一个 Daily 触发器
powershell -ExecutionPolicy Bypass -File scripts\run-daily.ps1 -Status
schtasks /Query /TN "Future-Workbench-Daily-20" /FO LIST /V

# 看运行历史：每条都带「触发」标记
python -c "import json;[print(r['startedAt'], r.get('trigger'), r['status'], r['newItems']) for r in json.load(open('web/data/logs/runs.json',encoding='utf-8'))[:10]]"
```

「采集与运行」页的运行历史里，每条都标了触发方式：

- **定时** —— 由任务计划程序在 20:00 触发（环境变量 `FUTURE_TRIGGER=scheduled` 标记）
- **手动** —— 你或我执行 `run-daily.ps1` / `collect.py`
- **调试** —— `--only`、`--dry-run` 这类部分运行，带「部分渠道」附注

> 这段说明是**补的**：第一版没有区分触发方式，开发当天我为了调试跑了十几次，
> 历史记录看起来就像"定时任务在几分钟内重复触发"。任务本身没问题（已核实
> 只有一个 Daily 触发器、`MultipleInstances=IgnoreNew`、上次返回码 2 = 部分渠道失败但数据已写入）。
> 现在每次运行都会记录 `trigger`，这类误会不会再发生。

**为什么 `status` 常显示 `partial` 而不是 `ok`**：有 10 个渠道是确认不可用的
（登录墙 / WAF / 机器人挑战），它们每次都会被如实记为 `blocked`，所以整轮状态是
`partial`。这是设计上的诚实，不是故障。

**关于登录类渠道**：按你的要求**暂不采集**。`inbox` 渠道已在配置里关掉
（`CHANNEL_SPECS["inbox"].disabled = True`），每日运行不会包含它。
将来要启用：把导出文件放进 `web/data/inbox/` 然后
`python scripts\collect.py --only inbox`，或在 `config/collector.config.json` 里设
`"enableInbox": true`。

---

## 三、目录结构

```
DS-daily/
├─ web/                        前端（零依赖、零构建）
│  ├─ index.html               应用外壳 + SVG 图标精灵
│  ├─ assets/css/theme.css     设计令牌：6 套主题 × 6 种强调色
│  ├─ assets/css/app.css       布局、组件、动画、响应式、打印样式
│  ├─ assets/js/app.js         ★ 生成产物（不要手改）
│  ├─ assets/js/*.part.js      真正的源码，按模块拆分
│  └─ data/                    数据层（前端唯一的数据来源）
│
├─ scripts/
│  ├─ collect.py               每日采集器（仅标准库，1600+ 行）
│  ├─ inbox.py                 登录类渠道的人工导入器
│  ├─ selfcheck.py             数据完整性/真实性闸门
│  ├─ run-daily.ps1            ★ 定时任务唯一入口（ASCII-only 源码）
│  ├─ daily-agent.md           Agent 深读层的任务说明
│  ├─ build.mjs                把 *.part.js 拼成 app.js（含模块语法守卫）
│  └─ check-parts.mjs          逐个 part 做真实解析校验
│
├─ config/collector.config.json  采集参数、关键词、权重（可随时调）
├─ docs/信息架构与数据契约.md    ★ 任务一的分析与所有设计决策
├─ research/                     调研原始产物与渠道实测证据
└─ web/data/logs/                运行日志
```

---

## 四、网页功能

| 视图 | 内容 |
|---|---|
| **今日工作台** | 问候 + 关键指标、今日高相关、近 7 日采集量、本周学习安排、岗位投递漏斗、数据层健康、分类分布 |
| **每日更新流** | 按日期/分类分组的时间线，每条带相关度、来源、难度，支持多维筛选 |
| **知识分类** | 14 个分类的采集目标、更新节奏、掌握进度；渠道优先级表；去重与更新规则说明 |
| **知识卡片** | 全部卡片，按相关度/时间/难度排序，可叠加分类、来源、标签、阅读状态筛选 |
| **公式剖析** | 每个公式五层：公式排版 → 符号表 → 推导链条 → 生活类比 → 可运行代码 → 面试易错点（内置 6 个高频公式：注意力、DPO、GRPO、DDPM、RoPE、CLIP） |
| **题库定位** | 手撕题 / 笔试场景题，主题热力与掌握度，逐题得分要点、踩坑、参考实现 |
| **仓库与课程** | 固定仓库（含学习计划与检查点）、课程、必读论文（按阅读顺序） |
| **岗位看板** | 投递漏斗看板（可拖拽切换阶段）、机会清单、按梯队排序、详情含面试流程与来源 |
| **技能矩阵** | 32 项技能 × 岗位重要度 × 我的掌握度，直接算出该补哪个缺口 |
| **学习路线** | 分方向模块化路线：任务 + 交付物 + 验收标准，可勾选进度；间隔重复与笔记模板 |
| **进度与统计** | 分类掌握度、每日采集量、热度日历（90 天）、来源分布、收藏概览 |
| **采集与运行** | 运行历史、渠道清单与实测状态、定时任务信息、真实性自查说明、自我迭代机制 |

**交互**：`Ctrl/⌘ + K` 全局搜索与命令面板 · `/` 快速搜索 · `g d/k/j/r/p` 跳转 ·
`Esc` 关闭 · 主题与强调色可切换 · 舒适/紧凑双密度 · 一键导出进度 JSON · 打印当前视图。

---

## 五、数据层契约（新增字段只需改两处）

前端**只**从 `web/data/` 读取，所有读取集中在 `core.part.js` 的 `Store` 对象里，
任何一个文件缺失或字段缺失都会降级显示而不是白屏。

| 文件 | 作用 |
|---|---|
| `manifest.json` | 首页状态条、健康面板、目标日期倒计时、下次运行时间、数据新鲜度 |
| `digest/today.json` | 今日增量 + **今日速览（TL;DR）**，更新流优先读它 |
| `items/index.json` | 全量知识卡片（搜索、筛选、统计的数据源） |
| `taxonomy.json` | 分类体系（「知识分类」页） |
| `sources.json` | 渠道清单 + 实测状态 + 备用源使用记录 + 打分/去重参数 |
| `jobs.json` | 岗位库（人工整理 + 每日增量） |
| `learning.json` | 学习路线 / 论文 / 仓库 / 课程 / 里程碑 / 学习系统 |
| `formulas.json` | 公式库（与前端内置种子合并去重） |
| `proposals/latest.json` | 自我迭代提案（渠道健康、关键词精简建议、人工复核队列） |
| `logs/runs.json` | 运行历史（「采集与运行」页） |

完整字段定义见 **[docs/信息架构与数据契约.md](docs/信息架构与数据契约.md#32-knowledge-item-契约前端唯一依赖的字段集)**。

**加一个新字段的流程**：采集器 `normalize()` 里加 → `build_all_items()` 的 `keep` 元组里加 →
（可选）某个视图渲染它。前端不需要重新构建（除非你直接改 `*.part.js`）。

---

## 六、登录类渠道：人工在环（当前已暂停）

小红书、BOSS直聘、拉勾、实习僧的 `robots.txt` 明确禁止自动化抓取，且抓取会触发账号风控。
本工作台**不抓这些站点**，改为人工导入，对它们的网络请求数为 0：

```powershell
# 1) 生成一份示例文件，照着格式填
python scripts\inbox.py --template

# 2) 编辑 web/data/inbox/manual.jsonl（每行一个 JSON 对象）
#    也可以放 manual.csv / manual.json，列名支持中文（标题/链接/公司/城市/薪资/截止…）

# 3) 规范化导入
python scripts\inbox.py -v

# 4) 并入知识库（下次 run-daily 也会自动带上）
python scripts\collect.py --only inbox
```

导入的内容会以 `manual` 渠道出现，来源标签保留你自己写的 `source`（如「BOSS直聘」），
在界面上与自动采集的内容可区分。**不需要**把账号密码给我或写进任何文件。

---

## 七、维护与调参

```powershell
# 调整关注方向的关键词与权重
notepad config\collector.config.json

# 改完权重要让既有语料重算分数（否则历史条目的打分仍是旧公式的结果）
python scripts\collect.py --rescore

# 只跑某几个渠道（调试；--only 会故意连 blocked 渠道也跑，用于复测）
python scripts\collect.py --only arxiv,hf_papers --limit 10 -v

# 渠道可达性探针（会留证据到 research/channel_probe.json）
python scripts\probe-channels.py
python scripts\collect.py --probe          # 只发一次请求/渠道，不写数据

# 重建前端产物（改了 *.part.js 之后必须执行）
node scripts\build.mjs

# 校验前端各部分语法（不要只依赖 app.js 的报错，它的行号会被模板字符串带偏）
node scripts\check-parts.mjs

# 数据层自检
python scripts\selfcheck.py

# 对着原始需求逐条审计（55 项可检验的断言）
python scripts\audit_requirements.py

# 把调研注册表（91 渠道 / robots 证据）合并进 web/data/sources.json，
# 同时保留采集器最新一轮的实测状态
python scripts\merge_registry.py

# 浏览器端全路由验证（会写 docs/screenshots/*.png 与 verify-report.json）
node scripts\verify-web.mjs
$env:WEB_ROOT="$PWD\dist"; node scripts\verify-web.mjs --port 8792   # 验证发布产物

# 重建学习路线 / 公式库（内容在脚本里，改完重新生成即可）
python scripts\build_learning_kb.py
python scripts\seed_formulas.py

# ---- 知识卡片的三道清理工具（都是「先报告、后执行」）----

# 1) 卡片自查：给每张卡打重要度分（core / useful / noise），列出建议下架项
python scripts\prune_items.py --show 20          # 只看报告
python scripts\prune_items.py --apply            # 执行：归档 + 写 blocklist

# 2) 第二轮深度去重：同一事物的多个版本（论文 v1/v2、模型 -MLX/-NVFP4 重打包）
python scripts\dedupe_deep.py --show 20
python scripts\dedupe_deep.py --apply

# 3) 深度解析队列的取样工具（跑验证子集，不动完整队列）
python scripts\scope_deep_read.py                    # 看当前队列
python scripts\scope_deep_read.py --per-category 1   # 每类取 1 条做验证
python scripts\scope_deep_read.py --restore          # 还原完整队列

# ---- 排期与发布 ----

# 改每日执行时间（会把 12+ 个文件里的时间一次性改完并自检）
python scripts\set_schedule_hour.py --to 20
python scripts\set_schedule_hour.py --to 20 --check

# 生成可公开发布的静态站点 + 隐私审计
python scripts\build_site.py

# 一键构建 + 审计 + 暂存，并打印 GitHub Pages 步骤
powershell -ExecutionPolicy Bypass -File scripts\publish-github-pages.ps1
```

> **为什么 `.ps1` 里没有中文**：Windows PowerShell 5.1 用系统 ANSI 代码页解码
> **无 BOM** 的 `.ps1`，任何中文字面量都会被解成非法语法（表现为
> `Missing closing }`）。所以 `run-daily.ps1` 与 `publish-github-pages.ps1`
> 保持纯 ASCII，所有中文输出交给 Python 脚本（`handoff_publish.py` 等）打印。
> 改这两个文件时请守住这条规则。

> **清理工具必须写 blocklist**：`prune_items.py` / `dedupe_deep.py` 下架的条目
> 会记入 `state/collector-state.json` 的 `blockedIds` / `blockedTitleKeys`，
> 采集器在**导入阶段**和**重建索引阶段**各查一次。少了任何一道，下架的条目
> 都会在下一轮 20:00 自动复活（这个坑真实发生过：库从 554 反弹回 763）。

**可选一：让 GitHub 渠道更快**。设置 `GITHUB_TOKEN` 可把搜索配额从 10 次/分钟提到 30 次/分钟：

```powershell
[Environment]::SetEnvironmentVariable('GITHUB_TOKEN','ghp_xxx','User')
```

**可选二：开启 Agent 深读层**（第 2 层）。它需要 LLM 凭据，而**计划任务不会继承你交互式 shell 的环境变量**，所以必须持久化设置，否则每天的日志里会看到 `layer 2: SKIPPED - no LLM credential`：

```powershell
# 方式 A：写入用户级环境变量（对计划任务生效，设完需重新登录或重启任务计划服务）
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY','sk-xxx','User')

# 方式 B：在 DSH Web 的 Models 页面填写一次，由凭据服务保存（推荐，不用重启）
```

深读层失败或未配置时，第 1 层的采集数据照常写入，工作台正常更新 —— 只是缺少
「为什么重要」的深挖、公式剖析与易读化解释。

---

## 八、深度解析配额与质量闸门

要求是「每类 2–10 条、都要有深度解析」。这件事拆成**两步**，因为「采集」与
「决定什么值得深读」是两个不同的问题：

```
collect.py        采集 → 规范化 → 去重 → 相关度打分 → 落盘（全部保留，可搜索）
      ↓
plan_deep_read.py 质量闸门 + 配额分配 → web/data/deep-read-plan.json
      ↓
Agent 深读层      只处理 queue 里的条目，每条必须产出图解 + 概念 + 公式
```

**质量闸门**（`scripts/plan_deep_read.py` 里的 `NOISE_RULES`）把这几类标为 `skimmed`，
不占深度解析名额，但仍留在库里可搜索、参与去重：

| 规则 | 挡掉的 | 为什么 |
|---|---|---|
| `career-vent` | 求职吐槽/情绪帖 | 信息量为零且大量重复 |
| `generic-question` | 泛化提问（「大家怎么看」） | 无法结构化成知识 |
| `referral-code` | 内推码广告 | 有用，但只需进岗位信息 |
| `salary-thread` | 纯薪资讨论 | 数字无法核实，极易重复 |

**配额分配**：按分类权重给名额（多模态/后训练/世界模型/手撕题 = 10 条，
生成式/RL = 9，Agent/工程 = 7，岗位/趋势 = 6），并按渠道去重，
避免一个话多的来源占满某个分类。实测：**708 候选 → 挡掉 16 条噪音 → 116 条队列（14 类）**。

**每张卡片在界面上标明自己的状态**，不再有「有的有解析、有的没有」的模糊感：

- `深度解析` —— 已有图解 + 概念易读化 + 公式
- `深读` —— 已深读但深度层未给图解
- `仅摘要` —— 还没深读；点开详情会明确说它是否在队列里、什么时候补上

**图解**是硬性要求（`daily-agent.md` 第 2 节）：深度层必须为每条产出
`diagram`，优先内联 SVG（自包含、无外链、颜色用主题变量），
或结构化 `spec`（`flow`/`architecture`/`curve`/`matrix`/`timeline`，前端用 CSS 画）。
SVG 在前端会**过一遍消毒**（去掉 `script`/`style`/`image`/`use`/`on*`/外链引用）
再注入，因为它来自数据文件。

---

## 九、发布到公网（手机也能看）

工作台是纯静态的（前端 + JSON 数据），任何静态托管都能用。一条命令产出发布目录：

```powershell
python scripts\build_site.py                    # 输出 dist/，约 2.4 MB
python -m http.server 8790 --directory dist     # 本地预览
```

`build_site.py` 用**白名单**复制，并自己审计隐私。**不会**进入 dist 的：

| 排除项 | 原因 |
|---|---|
| `web/data/state/collector-state.json`（约 1 MB） | 去重指纹与原始语料，内部状态 |
| `web/data/inbox/` | 你手工导入的小红书/BOSS直聘内容，可能含内推码与备注 |
| `web/data/logs/harness-*.log` | 本机运行日志（含绝对路径） |
| `*.part.js` | 前端源码，已在 `app.js` 打包，不必重复发布 |
| `items.json` | 与 `items/index.json` 字节相同，避免重复 1.1 MB |

发布方式任选（都免费）：

**A. Cloudflare Pages（推荐，国内访问相对稳）**
Workers & Pages → Create → Pages → Upload assets → 把 `dist/` 拖进去 → 得到 `*.pages.dev` 域名。

**B. GitHub Pages（当前已用这个方案发布）**

仓库：`https://github.com/eternallbk/future-platform`
站点：`https://eternallbk.github.io/future-platform/`

```powershell
# 一条命令：构建 dist/ → 隐私审计 → 推送到 gh-pages
powershell -ExecutionPolicy Bypass -File scripts\publish-gh-pages.ps1 -Rebuild
```

然后在 GitHub 网页端做**一次**设置：Settings → Pages → Source = `Deploy from a branch`
→ Branch = `gh-pages` / `(root)` → Save。

> **不要用 `git subtree push --prefix dist origin gh-pages`。** `dist/` 是生成目录、
> 被 `.gitignore` 排除，因此不是任何 commit 里的已跟踪路径；而 `git subtree split
> --prefix` 只读已提交历史，必然报 `fatal: 'dist' does not exist; use 'git subtree
> add'`；接着 `git subtree add --prefix dist` 又会因为 `dist/` 在磁盘上已存在而报
> `prefix 'dist' already exists`。两者都无法在 dist/ 被正确忽略的前提下工作。

> **部署仓库放在 `%TEMP%`，绝不放在 `dist/` 里 —— 这是踩过坑的。**
> 早期版本在 `dist/.git` 建了一个小仓库。当它丢失 HEAD/config 后就不再是有效
> 仓库，git 的目录发现机制会**向上走**并悄悄改用项目主仓库，结果：
> `git -C dist checkout -B gh-pages` 在主仓库里建了 `gh-pages` 分支并把工作区切过去、
> 四个 "Publish workbench site" 提交把源码（scripts/、web/data/、README.md）写进了
> 该分支、推送失败后主仓库停在错误分支上。当时网络不通所以没推上去，主仓库已复原。
> 现在的脚本：临时目录建仓库、每次 git 调用都显式设置 `GIT_DIR` + `GIT_WORK_TREE`、
> **校验解析出的仓库根目录必须等于临时目录，否则中止**、且再也不用裸 `-C dist`。

> **⚠️ 请提交你的工作。** 上面那次 `git checkout` 把**未提交**的改动回退了
> （`scripts/publish-gh-pages.ps1`、`scripts/handoff_ghpages.py`、
> `scripts/build_site.py` 的一部分、`run-daily.ps1` 的发布步骤都丢过一次）。
> 这些现在都已重新写好，但**仍未提交**。请尽快执行：
> ```powershell
> git add -A
> git commit -m "每日 20:00 排期、卡片清理、自动发布到 gh-pages"
> git push
> ```
> 规则：`main` 只放源码；任何未提交的新文件在切分支时都可能消失。

**C. Vercel / Netlify**：构建命令留空，发布目录填 `dist`。

### 安全措施（`build_site.py` 自动写入）

发布产物自带 `_headers`（Cloudflare Pages / Netlify 会读取生效）：

| 响应头 | 值 | 作用 |
|---|---|---|
| `Content-Security-Policy` | `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'` | 禁止一切外部请求；脚本/样式只能来自本站；不允许被 iframe 嵌入 |
| `X-Content-Type-Options` | `nosniff` | 防 MIME 混淆 |
| `Referrer-Policy` | `no-referrer` | 不外泄来源 |
| `X-Frame-Options` | `DENY` | 防点击劫持 |
| `Permissions-Policy` | 关闭地理位置 / 摄像头 / 麦克风 | 最小权限 |

CSP 能这么严，是因为工作台**完全自包含**：没有 CDN、没有外部字体、没有统计脚本、
没有远程图片（图解都是内联 SVG），只 `fetch` 自己目录下的 JSON。
我实测过在这套 CSP 下 13/13 路由正常渲染。

`robots.txt` 写的是 `Disallow: /` —— 个人工作台不需要被搜索引擎收录。

> **GitHub Pages 的限制**：它不读 `_headers`，无法设置响应头。
> 如果你在意安全头就用 Cloudflare Pages；只想图省事，GitHub Pages 也可用
> （风险面本来就很小：纯静态、无后端、无用户输入入库、无外部请求）。

> **注意**：站点是静态快照。手机看到的内容 = 最后一次 `build_site.py` 的数据。
> 想每天自动更新，就在 `run-daily.ps1` 收尾处加一行 `python scripts\build_site.py`，
> 再让 Cloudflare Pages / GitHub Pages 连仓库后自动部署。
>
> **隐私**：`inbox/` 与 `state/` 已排除，但**你自己新加的数据文件默认不发布** ——
> 要发布需在 `build_site.py` 的 `DATA_FILES` 里显式登记。
> 另外 `enrichment.json` 的 `selfCheck.uncertain` 是会发布的，别把私人备注写进去。

---

## 十、已知边界（诚实清单）

| 事项 | 现状 |
|---|---|
| arXiv / HF Daily / HF 模型 / GitHub 搜索 / GitHub 趋势 / HN / 量子位 / 雷锋网 / HF Blog / OpenAI News / 腾讯招聘 / DeepSeek / 牛客 / 知乎（镜像）/ 人工导入 | 实测可用，正常入库（截至 2026-10-01 共 743 条） |
| OpenReview | `/notes` 与 `/notes/search` 均返回机器人挑战页（HTTP 200 但内容是 "Verifying your browser"）→ 已列入 `DISABLED_CHANNELS`，不再每天重试 |
| Reddit | `.json` 403，`.rss` 可读但 `robots.txt` 是 `User-agent: * / Disallow: /` → **合规原因不抓取**；社区信号由 Hacker News 承担 |
| 小红书 / BOSS直聘 / 拉勾 / 实习僧 / 猎聘 / 脉脉 | `robots.txt` 禁止或需登录 → 人工在环（[操作手册](docs/登录类渠道操作手册.md)），对它们的网络请求数为 **0**。**请不要提供账密**：自动登录违反服务条款、平台有能力关联处罚，且技术上绕过滑块/验证码既不稳定也不正当 |
| rsshub.app 公共实例 | Cloudflare 403；可行路径是自建 Docker 实例 |
| 字节 / 阿里 / 智谱 / 月之暗面 / MiniMax / 上海AI Lab 招聘 | 接口被 WAF 拦或岗位列表由 JS 渲染 → 依赖人工导入（已在注册表标注证据与状态码） |
| 机器之心 RSS | 该地址返回 HTML 而非 feed（"假 200"）→ 已由雷锋网替代 |
| Papers With Code | 已下线（302 到 HuggingFace） |
| `learning.json` | 已按你的起点重排：8 条路线 / 70 个任务 / 14 个里程碑（含手撕题与论文讲述两条针对性路线） |
| Agent 深读层 | 已跑通。凭据可从环境变量或 harness 凭据库读取；缺凭据时明确跳过并写日志 |
| 深度解析覆盖率 | 当前 743 条中 18 条已深读（其中 6 条有图解）。深度层按队列推进（116 条），每轮补齐一批；`仅摘要` 标记会一直显示到该条被深读 |

失败都会被如实记录：`manifest.json.status`、`logs/runs.json` 的 `notes` 字段、
`selfcheck.py` 的告警，以及「采集与运行」页的渠道状态表。
