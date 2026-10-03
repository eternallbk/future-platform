# {{DATE}} · Future 工作台每日深读任务

你正在为「Future · 求职学习工作台」执行每日常规深读。工作目录 `{{ROOT}}`。
确定性采集层已经跑完，原始数据已经落盘。你的工作是**在已有数据上做归纳、剖析与易读化**，
并把结果写回数据层。

---

## 0. 铁律（违反即视为任务失败）

1. **禁止编造。** 你写的每一条结论都必须能在 `web/data/items/index.json` 的标题/摘要里
   找到依据。不确定就写「待核实」，绝不猜。
2. **禁止编造 URL。** 只能使用数据里已有的 `url` / `sources[].url`。不要"推测"论文链接。
3. **不抓取需要登录的站点**（小红书 / BOSS直聘 / 拉勾 / 实习僧）。若需要其中内容，
   在提案文件里写明"需人工导入"，不要尝试自动化。
4. **不改前端代码。** 只写 `web/data/` 下的文件，且只写下面列出的文件。
5. **不减小已有数据。** 写 JSON 前先读旧文件，做合并而不是覆盖。
6. **UTF-8 无 BOM。** 用 Python 写文件（`json.dump(..., ensure_ascii=False)`），
   不要用 PowerShell 的 `Set-Content` / `>`（会写 BOM 并可能损坏中文）。

---

## 1. 读入

1. 读 `web/data/manifest.json`：拿到本轮 `newItems`、`status`、失败的渠道列表。
2. **读 `web/data/deep-read-plan.json` —— 这是你的工作清单，不要自己另挑条目。**
   它由 `scripts/plan_deep_read.py` 生成，包含三个清单：
   - **`queue`**：**尚未深读**的条目。已做质量闸门（求职吐槽、泛化提问、内推码广告、
     纯薪资讨论标为 `skimmed`，**不要**深读，它们仍留在卡片库可搜索、参与去重）
     与配额分配（按分类 2–10 条）。
   - **`backfill`**：**已经深读但解析不完整**的条目（缺图解 / 缺公式 / 缺 `teaching`）。
     这些是"有的卡片有解析、有的只有摘要"的历史遗留。**必须一并补全**：
     按 `missing` 字段补齐缺的那几层，不要重写已有内容（合并而非覆盖）。
     `backfillWeight` 越高越优先。
   - **`problems`**：**题库定位的待解析题目**（读者明确要求的那一层）。
     **优先级最高：先把 `problems` 做完，再去做 `queue` / `backfill`。**
     理由：卡片深读是"锦上添花"，而一道题没有题解就是读者明确抱怨过的"只有一个链接"。
     每条是一个**具体题目**（`id / kind / title / statement / topics / sourceUrl`），
     你要为它写一份**题解**：题意澄清 → 思路（暴力→最优）→ 复杂度 → **可运行代码** →
     图解 → 易错点/边界 → 面试追问与变形。**源页面没有解析也要自己写**——
     这是读者的原话要求，也是这份清单存在的唯一理由。产出到 `out-prob-*.json`（见 2.6）。
   三个清单**都要**处理，**每一条都必须产出完整解析（含图解，见第 2 节）。**
   两者都为空时直接跳到第 5 步（只写运行备注）。
3. 读 `web/data/digest/today.json` 与 `web/data/items/index.json` 取这些条目的原文摘要。
4. 读 `config/collector.config.json` 了解关注方向与权重。
5. 读 `web/data/formulas.json` 已有的公式 id，避免重复造。

> 为什么改成「按清单做」而不是「自己挑最相关的 8–12 条」：
> 质量优先需要**可复核**的选择依据。清单是确定性生成的，你可以质疑它、人工改它，
> 下一轮还能对比「计划 vs 实际」来评估筛选规则好不好。

---

## 2. 深读：每条都必须有「图解」，并且要**教会**人

只写摘要等于没做。每条深读必须同时满足三件事：

1. **有图解**（下面给出 schema）；
2. **有 `teaching` 讲解层**（下面给出 schema）—— 用**多种方式**讲同一件事；
3. 讲解与图解**互相配合**：图负责建立直觉，文字负责说清机制与边界。

### 2.0 `teaching`：用至少 3 种不同方式讲同一件事

要求「讲解、教学、官方口径、你自己的思考」结合。不要只挑一种方式 ——
同一个概念，用不同角度讲，读者才可能在面试里讲清楚。**至少选 3 种**，
并且必须包含 `mechanism`、`boundary`、`analogyBoundary` 三项（它们是"真懂"的证据）：

```jsonc
"teaching": {
  // ① 由浅入深：先给一个能听懂的版本，再逐层加细节
  "progressive": [
    {"level": "一句话", "text": "≤40 字，外行也能听懂"},
    {"level": "入门",   "text": "≤120 字，有深度学习基础即可"},
    {"level": "进阶",   "text": "≤200 字，说清关键设计选择与取舍"},
    {"level": "专家",   "text": "≤260 字，与其它方法的对比、适用边界"}
  ],
  // ② 苏格拉底式追问：用问题链把读者带到答案
  "socratic": [
    {"q": "如果不做这一步会怎样？", "a": "≤100 字"},
    {"q": "换成更简单的做法为什么不够？", "a": "≤100 字"}
  ],
  // ③ 机制：关键设计 + 它解决了什么（必填）
  "mechanism": "≤200 字，说清『为什么必须这样设计』，而不是『它做了什么』",
  // ④ 边界：什么时候不适用、什么情况下会失效（必填）
  "boundary": "≤160 字，失效条件与适用前提",
  // ⑤ 类比的失效点：类比在哪里不再成立（必填，比类比值钱）
  "analogyBoundary": "≤140 字",
  // ⑥ 常见误解：面试里容易被扣分的错解
  "misconceptions": ["≤80 字/条，2-3 条"],
  // ⑦ 你自己的思考：把摘要信息与已知方法联系起来，或指出可疑之处
  "ownAnalysis": "≤220 字。允许推理，但必须标明是推理而非原文结论",
  // ⑧ 官方/权威口径：只引用数据里已有 url 的来源；没有就写空数组
  "officialNotes": [{"position": "论文/官方怎么说", "source": "来自数据里的 url 或来源名"}],
  // ⑨ 面试怎么答：一段可以照说的答法
  "interviewAnswer": "≤200 字，第一人称、像在面试现场说出来",
  // ⑩ 学习建议：读它之前该先会什么、读完该练什么
  "studyPath": {"prerequisites": ["..."], "nextSteps": ["..."]}
}
```

**纪律**：
- `ownAnalysis` 里凡是推断，必须显式写「（推断）」或「（待核实）」，不要冒充原文结论。
- `officialNotes` **只能**用数据里已有的 `url`/来源名；编链接视为任务失败。
- 讲不清的部分，就在 `selfCheck.uncertain` 里写「待核实」，**不要用空话填满**。
- 不要把整段摘要抄进 `progressive[3].text`；那是采集层已经做的事。

### 2.1 `diagram`（硬性要求）

**`queue` 与 `backfill` 里每一条都要产出 `diagram`。** 图解必须**自包含**：
用内联 SVG 或结构化描述，**不要**依赖外部图片（无法离线、无法版本控制、链接会烂）。

```jsonc
"diagram": {
  "kind": "flow | architecture | curve | matrix | timeline",
  "title": "图的中文标题（≤20 字）",
  "caption": "一句话说明这张图想让你记住什么",
  // 方式 A（优先）：内联 SVG。必须自包含：viewBox、无外部引用、
  // 颜色只用 currentColor / var(--accent) / var(--fg-1) / var(--line-1)
  "svg": "<svg viewBox='0 0 480 200' xmlns='http://www.w3.org/2000/svg'>...</svg>",
  // 方式 B：结构化描述，前端按 type 用 CSS 画。适合流程/对比/时间线
  "spec": {
    "type": "flow",
    "nodes": [{"label": "输入", "detail": "图像 patch"}, {"label": "编码器", "detail": "ViT-L"}],
    "edges": [{"from": 0, "to": 1, "label": "patch 化"}]
  },
  "alt": "给读屏软件的文字替代（必填，一行）"
}
```

**画图纪律**：
- SVG 里不要出现 `<image>`、外链 `<use>`、`style` 里的外部 `url()`。
- 宽高比控制 16:6 ~ 16:9，`viewBox` 留 12px 边距，字号 ≥ 13（手机上要看得到）。
- 颜色只用主题变量或 `currentColor`，六套主题都要能正常显示。
- `spec.type` 只能是 `flow` / `architecture` / `curve` / `matrix` / `timeline`。
- 画不出来**不要编**：宁可用 `spec` 画一个朴素流程，也不要塞一个自己都解释不了的复杂 SVG。

对清单里的每条，产出一个 `enrichment` 对象：

```json
{
  "id": "<与 index.json 中完全一致的 id>",
  "tldr": "一句话结论，≤60 字，中文，直接说它做了什么/结论是什么",
  "keyPoints": ["3-5 条要点，每条 ≤40 字，必须来自原文摘要"],
  "why": "为什么对『多模态/后训练/生成式/世界模型』方向求职者重要，≤80 字",
  "difficulty": "easy|medium|hard",
  "tags": ["3-6 个英文小写标签"],
  "entities": ["方法名/机构名/模型名，来自原文"],
  "diagram": { "kind": "...", "title": "...", "caption": "...", "svg": "...", "spec": {}, "alt": "..." },
  "concepts": [
    {
      "name": "抽象概念名，例如 Group Relative Policy Optimization",
      "readable": "用大白话解释，≤100 字，面向有深度学习基础但没读过这篇论文的人",
      "analogy": "一个生活类比，必须具体、可想象，不要用『就像优化一样』这类空话",
      "analogyBreaksDown": "这个类比在什么地方失效 —— 比类比本身更重要，逼你说出真实边界",
      "mechanism": "机制：关键设计选择与它解决了什么，≤120 字",
      "visual": "用纯文本描述一个可视化方案：画什么轴、什么形状、什么颜色代表什么",
      "prerequisites": ["读它之前需要会什么"],
      "selfTest": ["3 道自测题，能答对才算学会"],
      "formula": "如果该概念有核心公式，给出 LaTeX 与符号表；没有则留空字符串"
    }
  ],
  "formulas": [
    {
      "id": "f-<slug>",
      "name": "公式中文名（英文名）",
      "category": "multimodal|posttraining|worldmodel|generative|rl|agent|foundation|engineering",
      "latex": "LaTeX",
      "symbols": [["符号", "含义"]],
      "derivation": ["逐步推导，2-5 步，每步一句话说清动机"],
      "analogy": "生活类比",
      "pitfalls": ["面试易错点"],
      "code": "可运行的 PyTorch/Python 片段（若公式可代码化）",
      "sources": [{"title": "原文标题", "url": "必须来自数据里已有的 url"}]
    }
  ],
  "selfCheck": {
    "claims": ["你能从摘要中直接证实的断言，逐条列出"],
    "uncertain": ["你不确定、需要人工核实的地方，逐条列出；没有就写空数组"]
  }
}
```

**公式剖析必须四层齐全**：符号表 → 推导链条 → 生活类比 → 可运行代码 → 易错点。
这是工作台「公式剖析」页面的核心价值，缺层会被视为未完成。

---

## 2.6 题库定位：把题目变成题解（`problems` 清单）

读者的原话：「对于算法题和仓库的整理，直接给出仓库链接虽然可以保留，但我更希望你能根据题库
信息，阅读并自己深度解析相关手撕题、算法题等具体问题并整理到题库定位中附带深入的代码解析或
配图解析等」。所以在 `problems` 清单里，**链接不算交付**，题解才算。

### 交付物
每个题目写一个文件：`scripts/agent-work-{{DATE}}/out-prob-<两位数序号>.json`，
形状固定为 `{"batchId": "prob-01", "entries": [ <题解对象> ]}`，一个文件一条（便于并行与复核）。

### 题解对象 schema
```jsonc
{
  "id": "pb-<itemId> 或人工题 id（必须与清单里的 id 完全一致，不要自己改）",
  "itemId": "如果清单给了 itemId 就带上（采集题才有）",
  "tldr": "一句话结论 ≤60 字：这题考什么、最优解是什么",
  "restated": "题意澄清 ≤140 字：输入/输出/约束/样例。题面被截断就写清楚截断在哪",
  "approach": [
    {"step": "暴力", "detail": "枚举什么、为什么不够（复杂度）"},
    {"step": "最优", "detail": "用什么观察/数据结构把复杂度降下来"}
  ],
  "complexity": {"time": "O(n)", "space": "O(1)", "why": "代价来自哪里 ≤80 字"},
  "code": {
    "language": "python",
    "source": "完整可运行的 Python 实现（含输入解析或函数签名 + 必要的边界处理）",
    "walkthrough": [{"note": "这一段在做什么（逐块讲，不要逐行念代码）"}]
  },
  "edgeCases": ["空输入", "n=1", "全相同元素"],
  "pitfalls": ["面试现场最容易写错的地方"],
  "followUps": ["面试官可能的追问（复杂度再降/换数据结构/改成在线）"],
  "variations": ["同源变形题"],
  "diagram": { "kind": "flow|architecture|curve|matrix|timeline", "title": "≤20 字",
               "caption": "这张图让你记住什么", "svg": "<svg viewBox='0 0 480 240' ...>", "alt": "一行替代文字" },
  "selfTest": ["2-3 道自测题，能答对才算会"],
  "keyPoints": ["得分要点（≤5 条）"],
  "sources": [{"title": "题目出处", "url": "只能用清单里的 sourceUrl 或题面里已有的链接"}],
  "selfCheck": {"claims": ["能从题面/公开定义直接证实的断言"], "uncertain": ["题面被截断、样例缺失、复杂度依赖假设等，逐条列出"]}
}
```

### 硬性纪律
- **代码必须是真的**：能跑、有边界处理、≥ 8 行有效逻辑。禁止 `pass` / `...` / `# TODO` 占位
  （`validate_problem_analysis.py` 会拒绝）。
- **思路必须写「为什么」**：不只写"用哈希表"，要写"因为要在 O(1) 内判断补数是否出现过"。
- **图解是硬性要求**：每道题都要有一张自包含 SVG（规则同 2.1：viewBox、无 `<image>`/`<script>`、
  字号 ≥13、只用主题色、16:6~16:9）。流程图/状态转移图/双指针移动图/递归树，选最能说清的那个。
- **禁止编造 URL**：`sources[].url` 只能是清单里的 `sourceUrl`，或题面文本里已经出现的链接。
- **人工题（`origin: "curated"`）不要重写人工内容**：`has` 里为 `true` 的层已经有人写了，
  你只补缺的（通常是 `code` / `diagram` / `complexity` / `approach`），并保持原文观点。
- **题面不足就直说**：只从题面能确定的写进正文，其余进 `selfCheck.uncertain`，不要脑补样例。
- 校验：`python scripts/validate_problem_analysis.py --date {{DATE}} --strict`（必须 0 error；
  警告也要求清零）。`apply_enrichment.py` 会把 `out-prob-*.json` 合并成
  `web/data/problem-analysis.json`，并把采集题同步投影成一张轻量卡片（卡片库也会显示「已深读」）。

### 配额与优先级
`problems` 清单已经按「有完整题面的手撕题 → 人工手撕题 → 笔试场景题」排好序，
**按顺序做完清单内的题**（默认每天 20 道配额，由 `plan_deep_read.py --problems-max` 决定）。
做不完就在 `skips.json` 里记一条 `{"rule":"budget","ids":[...],"why":"本轮预算内未完成，次日继续"}`，
**不要**为凑数写空洞题解。

---

## 3. 岗位信息合并

如果本轮 digest 里出现招聘类条目（`category == "job"`），或字节/腾讯/阿里等招聘接口
返回了新的算法实习岗位：

1. 读 `web/data/jobs.json`（已有人工整理 + 每日增量的岗位库）。
2. 把新岗位**追加**到 `jobs` 数组，字段与既有条目保持一致
   （`id, company, title, directions, cities, pay, duration, conversion, open, applyUrl, sources, relevance`）。
3. `id` 用 `slug(company)-slug(title)` 保证稳定，重复岗位不要重复插入。
4. `directions` 只能用这些取值：`multimodal, post-training, world-model, generative, rl, agent, infra, embodied`。
5. 岗位信息里任何数字（日薪、名额、截止时间）若无法从原文确认，写 `null` 并在 `notes` 里标"待核实"。

---

## 4. 提案（自我迭代）

写 `web/data/proposals/{{DATE}}.json`：

```json
{
  "date": "{{DATE}}",
  "generatedBy": "daily-agent",
  "channelHealth": [{"id": "openreview", "status": "blocked", "evidence": "HTTP 403", "suggestedAction": "改为人工导入或寻找替代源"}],
  "taxonomyProposals": [{"action": "add|split|merge|reweight", "category": "worldmodel", "reason": "...", "proposedKeywords": ["..."]}],
  "keywordProposals": [{"category": "posttraining", "add": ["..."], "remove": ["..."], "reason": "近 7 日本方向命中率偏低/噪音偏高"}],
  "newChannelProposals": [{"id": "...", "why": "...", "verifiedUrl": "...", "authRequired": false}],
  "noiseReport": {"overBroadKeywords": ["..."], "underCoveredCategories": ["..."]},
  "humanReviewQueue": ["需要我（人类）确认的事项，逐条写清楚要确认什么"]
}
```
所有提案都只是**建议**，绝不自动生效；它们在「采集与运行」页面显示，等人工确认。

---

## 5. 写回

用 Python 脚本（放在 `scripts/` 下，例如 `scripts/apply_enrichment.py`，可复用）完成：

1. **`web/data/enrichment.json`** — 形如 `{"generatedAt": "...", "byId": {"<item id>": {...enrichment...}}}`。
   读旧文件后合并（新值覆盖同 id 的旧值），不要丢历史。
2. **`web/data/formulas.json`** — `{"generatedAt": "...", "source": "seed+daily", "formulas": [...]}`。
   读旧文件，按 `id` 去重后追加本轮的新公式；保留所有已有公式。
3. **`web/data/problem-analysis.json`** — 题库定位题解，形如
   `{"generatedAt": "...", "source": "seed+daily-agent", "count": N, "byId": {"<题目 id>": {...题解...}}}`。
   **由 `out-prob-*.json` 经 `apply_enrichment.py` 合并**（不要手写这个文件的关键结构）。
4. **`web/data/jobs.json`** — 见第 3 步（增量合并）。
5. **`web/data/proposals/{{DATE}}.json`** — 见第 4 步。
6. **`web/data/logs/agent-{{DATE}}.json`** — 运行记录：
   `{"date","startedAt","finishedAt","status","enrichedCount","formulaCount","problemAnalysisCount","newJobs","notes","errors"}`。

写完后运行校验：

```bash
python -c "import json,glob;[json.load(open(f,encoding='utf-8')) for f in glob.glob('web/data/**/*.json',recursive=True)];print('all json ok')"
python scripts/validate_agent_out.py --date {{DATE}} --strict
python scripts/validate_problem_analysis.py --date {{DATE}} --strict
```

---

## 6. 报告

最后用中文输出一份简短汇报（≤200 字 + 列表）：

- 本轮深读了几条、**题库定位写了几道题解**、新增几个公式、几个岗位；
- 数据层健康状况（哪些渠道失败、是否需要人工介入登录类渠道）；
- 你在 `selfCheck.uncertain` 里标记的、需要我确认的事项；
- 下一步建议（1–3 条，具体可执行）。

---

## 附：绝对不要做的事

- 不要把整篇论文摘要复制进 `tldr`（那是采集层已经做的事）。
- 不要生成"这条很重要，建议阅读"这类无信息量的句子。
- 不要为了凑数给低相关度条目写 enrichment。
- 不要把无法验证的内容写成确定语气。
- 不要在 `notes` 里写英文以外的花哨格式；工作台渲染的是纯文本/Markdown 子集。
