# Daily Brief · 信息茧房意识与突破系统

一个**适用于每个人**的信息检索推荐系统：它既能帮你**深入了解自己真正关心的核心喜好**，也能帮你**看见并主动打破信息茧房**——而不是无限喂养你已经认同的观点。

它每天从你可能一辈子都不会偶然刷到的信息源里，替你各挑选一批"值得读"的内容：一部分强化你的长期兴趣，一部分刻意把你推向茧房之外的角度。你只需每周点几次赞，系统就持续学习你的偏好，并在反方向不断给你抛出新的、对抗性的视角。

> **核心理念**：推荐系统不应只是"猜你喜欢"。真正负责任的推荐，是在**贴合偏好（放大）**与**突破茧房（对冲）**之间保持张力——让你既深耕，又不画地为牢。

---

## 目录结构

```
DailyBrief/
├── README.md                  # 本文件
├── IDEA.md                    # 项目愿景与设计理念
├── .env.example               # 环境变量模板（复制为 .env）
├── requirements.txt           # 依赖说明（核心脚本 stdlib only）
├── modules/                   # 核心逻辑模块（采集/过滤/排序/偏好）
│   ├── window.py              # 时间窗口计算（断档补采 + 72h 硬上限）
│   ├── retrieve.py            # 采集（HN + 多源搜索 + 正文抓取）
│   ├── filter.py              # 硬过滤（去重/字数/日期/AI 水文检测）
│   └── rank.py                # 排序 + 偏好方向/探索方向注入
├── scripts/                   # 运行脚本
│   ├── collect_brief.py       # 采集编排入口：窗口→去重→搜索→过滤→候选池
│   ├── validate_brief.py      # 强校验闸门（PASS 才投递）
│   ├── likes.py               # 点赞偏好模块：方向推断/样本门槛/偏好/数据驱动探索
│   ├── add_like.py            # 点赞 CLI：按序号/URL 写入点赞
│   ├── like_server.py         # 点赞 HTTP 服务：点链接即记录（按需启动）
│   ├── like_ctl.py            # 点赞服务控制：start/stop/status
│   ├── like_links.py          # 简报文末点赞区幂等重建（按当前参考资料整体重建）
│   └── replay_candidates.py   # 历史候选池离线回放（只读体检，不合格明确拒绝）
├── tests/                     # stdlib unittest 回归测试（mock 离线，python3 -m unittest discover -s tests）
├── docs/                      # 验收报告（流程加固验收.md 等）
├── data/                      # 运行时生成（已 gitignore）
│   ├── _dedup_urls.json       # 已推送 URL 去重集合（自动维护）
│   └── likes.json             # 点赞记录（用户偏好，驱动检索增强）
├── _candidates/               # 候选 JSON（过程文件，已 gitignore）
└── output/                    # 已发布日期入口及 runs/{run_id}/ 草稿/最终快照（已 gitignore）
```

> `modules/` 是采集端逻辑的模块化拆分（M1 阶段成果）；`scripts/collect_brief.py` 目前是总入口。

---

## 它的双重目标

### ① 深入理解你的核心喜好
每次你点赞，系统记录该文章的方向（AI/科技/商业/生活/健康/轨道交通）、关键词、话题。样本积累到阈值后，检索时**优先从你的偏好方向多挖内容**——让你越来越懂自己在读什么、在意什么。

### ② 看见并突破信息茧房
系统不满足于"喂你喜欢的"。它持续做三件事：
1. **识别你的茧房**：统计你的兴趣分布，定位你高度集中、很少接触莫方面的方向
2. **主动对冲**：把探索预算刻意投向你没在读的方向（跨界）与对立方立场（对抗视角）
3. **范式注入**：候选池为探索内容保底席位，校验闸门强制"本期必须有探索内容进简报"

**一句话**：主引擎照你说的方向挖，破茧引擎照你**没说的方向**扔过来。

---

## 工作流

### 批次隔离与可追溯交付（2026-10-10）

每次采集保存到 `_candidates/runs/{run_id}/`：`collected.json` 是不可覆盖的初始快照，
`Daily-Brief-{date}-candidates.json` 是审核/补抓工作副本，`manifest.json` 指定本次两个精确路径。
简报草稿放在 `output/runs/{run_id}/Daily-Brief-{date}.md`。采集 stdout 输出这两个路径，
Hermes 与 agy 必须使用它们，不能使用按日期的候选别名写作。

```bash
# 把采集输出的精确路径传给 agy；prompt 必须含两个路径、窗口和 run_id
python3 scripts/run_agy.py '<本次候选路径>' '<本次prompt文件>' --timeout 1200
# 主席读草稿、核对原文，等 agy 真正退出后统一交付
python3 scripts/finalize_brief.py '<本次简报路径>' '<本次候选路径>'
```

同批次 agy 与发布共享写入锁；等待窗口超时不代表进程结束，不能直接重试。
真实超时结束 agy 子进程组，退出码非零才允许重试。finalize 会预检、重建点赞区、复检，
保存最终候选和简报快照，再发布当日入口；已有当日发布版时拒绝被新批次覆盖。
交付使用它返回的 `MEDIA` 最终快照路径。

每次校验在批次的 `validation/` 下保留完整输入、哈希、run_id 和 PASS/FAIL 输出。
自动选池优先匹配简报 run_id 的快照；已发布快照优先于审核工作副本。
记录模块也核对 run_id 并保留精确路径与哈希。校验通过不等同于摘要事实已核实，
主席仍须读草稿与原文。Hermes 定时任务完整提示词见 [docs/hermes-dailybrief-task.txt](docs/hermes-dailybrief-task.txt)。

核心脚本独立存储批次；`_candidates/Daily-Brief-{date}-candidates.json` 仅是最新采集缓存，
`output/Daily-Brief-{date}.md` 是已发布版入口。旧数据不会自动冒充有快照的新批次。

```
cron（或手动）──▶ collect_brief.py（窗口→去重→搜索→硬过滤→候选池+偏好/探索标记）
                        │
                        ▼
                  LLM 读候选 JSON → 写简报 md
                        │
                        ▼
                  validate_brief.py（PASS 才投递）
                        │
                        ▼
                  like_links.py（简报文末追加点赞区）
                        │
                        ▼
                  交付（TLDR + 附件，含点赞区）→ 你点赞 → 偏好/探索引擎迭代
```

---

## 快速开始

### 1. 安装

核心脚本仅依赖 Python 3 标准库，无需第三方包。搜索通过可插拔的 CLI 完成（见"依赖"）。

```bash
git clone https://github.com/qinqiang316/Daily_brief.git
cd Daily_brief
python3 scripts/collect_brief.py          # 产出当日候选池
```

### 2. 写作与校验

```bash
# （LLM 读取 _candidates/*.json 撰写简报，此处为示意）
# 强校验闸门 —— 必跑
python3 scripts/validate_brief.py
```

### 3. 点赞（驱动偏好学习）

```bash
python3 scripts/like_ctl.py start                    # 启动点赞服务（空闲15分钟自动退出）
# 或文字指令（无需服务）：
python3 scripts/add_like.py --brief Daily-Brief-2026-08-19.md --num 3 5
python3 scripts/add_like.py --url https://xxx --title "标题" --direction 轨道交通
python3 scripts/add_like.py --list
python3 scripts/add_like.py --stats
```

点赞阈值（可配置）：**总点赞 ≥10 条 且 最热方向占比 ≥40%** → 才认定偏好方向并注入偏好查询；探索查询优先走数据驱动，样本不足时回退日期轮换。详见 `likes.py`。

---

## 防信息茧房（硬规则）

| 机制 | 实现 |
|------|------|
| 偏好不垄断 | 偏好方向最多 2 条定向查询（基础查询不变） |
| 数据驱动反方向突破 | 探索优先投空白/低关注方向；无空白时投茧房对立/陌生角度查询；样本不足回退日期轮换 |
| 探索保底进池 | 候选池为探索内容预留 ≤2 席（`MAX_EXPLORE_SEATS`），生活/健康方向优先；生活/健康有合格条目时各保底 ≥1 席（不限探索，计入主题与 HN 上限） |
| 探索必进简报 | validate 硬校验：候选有探索候选时，简报须收录 ≥1 篇 |
| 偏好封顶 | 深度总结区「（偏好命中）」条目 ≤8（`MAX_PREF_DEEP`） |
| 样本门槛 | 点赞 ≥10 且最热方向 ≥40% 才启用偏好优先（`MIN_LIKES_FOR_PREFERENCE` / `MIN_PREF_SHARE`） |

探索内容在简报标注 **`（今日探索）`**，偏好命中标注 **`（偏好命中）`**。

---

## 强校验闸门（P0）

写作后必跑，FAIL 即修正或放弃本期，禁止直接投递：

```bash
python3 scripts/validate_brief.py
```

校验项：① 简报 URL ⊆ 候选池；② 无历史复现；③ 日期未验证不得收录（含速览 leftover，出现即 FAIL）；④ 候选先生成后写简报；⑤ 探索条目必收；⑥ 偏好命中 ≤8；⑦ 简报与候选**按文件名日期严格配对**（跨日错配 FAIL），引用候选日期须日精度且落在候选窗口内、**真实 content 实测 ≥500 字**（word_count 元数据仅参考不得冒充证据）、必须带 `date_source` 日期来源（旧格式布尔位不能冒充验证）；⑧ 候选带 run_id 时简报**必须标注** `<!-- run_id: ... -->` 且一致。（本机点赞链接 127.0.0.1:8900 已忽略。）

采集端准入（2026-10-08 加固，所有来源同一标准、HN 不豁免）：正文 ≥500 字 + 发布日期证据精确到日 + 窗口内，缺一不入推荐池；日期证据链按 **JSON-LD → HTML meta → `<time datetime>` → 署名发布时间标记 → URL 日精度 → 标题日精度** 取证并记录 `date_source`/`date_evidence`/`date_precision`，普通正文第一处日期（常为事件时间）不得冒充发布时间，全部日期过合法日历校验；HN created_at 仅作热议日期，不充当发布日期；畸形 URL 拒收不静默修复；空池熔断不覆盖旧候选；采集有锁、原子落盘、`--reuse-cache` 显式同日缓存复用；正文抓取/存储上限 6000 字符（容下英文 500 词）；**生活/健康有合格条目时各保底 ≥1 席**（不限探索候选，计入主题与 HN 上限）；所有网络阶段共享一个 480s 墙钟截止时间，搜索阶段用派生子预算为正文阶段预留 150s，偏好/轨交/探索方向查询先行，查询临时文件走 tempfile 可指定目录且 finally 清理（单次 socket 超时钳制于剩余预算，属预算化约束而非数学严格硬截止）。历史候选池可用 `python3 scripts/replay_candidates.py <日期>` 离线回放体检。

---

## 依赖

| 依赖 | 说明 |
|------|------|
| Python 3 | 核心脚本仅标准库，无第三方包 |
| 搜索后端 | 可插拔 CLI（本仓默认 anysearch skill），脚本通过子进程调用，替换搜索后端只需改一处调用 |
| 时间窗口 | 断档补采 + 72h 硬上限，防陈旧内容混入 |

---

## 路线图

- [x] 采集脚本化（去重 / 候选池 / 硬过滤 / 强校验全脚本化）
- [x] 点赞偏好 + 防信息茧房（偏好/探索轮换、硬校验）
- [x] 核心逻辑模块化（modules/：window/retrieve/filter/rank）
- [ ] **新源发现**：跳出既有固定源，发现新增量信息源及其高质量文章（新增量方向）
- [ ] **细粒度偏好画像**：从 6 大方向细化到关键词/话题/学科级
- [ ] **反方向拓展**：基于兴趣分布，刻意推荐对抗性/陌生角度内容
- [x] 统一 CLI 入口（scripts/dailybrief.py，包含 collect/validate/finalize）

详见 [IDEA.md](IDEA.md) 了解完整设计理念与演进方向。

---

## 许可

本项目为个人研究/实验用途，欢迎借鉴思路。
