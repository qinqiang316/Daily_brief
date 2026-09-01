# DailyBrief 脚本变更记录

## 2026-09-01 — 首次代码质量检查与修复（小g执行，Hermes复核）

检查对象：`collect_brief.py`、`validate_brief.py`、`like_links.py`、`add_like.py`

| 指标 | 数量 |
| --- | --- |
| 发现问题 | 10 |
| 已直接修复 | 10 |
| 建议后续优化 | 1（`modules/retrieve.py` 临时查询文件清理） |
| 不建议变动 | 1（`modules/rank.py` / `modules/filter.py` 水文过滤与 72h 窗口规则） |
| 遗留阻塞风险 | 0 |

### add_like.py
- 修复 `find_brief(None)` 崩溃：安全处理 `None` / 空串参数，无匹配或无参时回退到最新简报，根除 `TypeError`
- `parse_brief_refs` 限定在「## 参考资料」段内提取，避免正文误伤
- Docstring 返回值说明修正为 `(ok, msg, dup)`

### like_links.py
- 修复无参执行级联崩溃（依赖 `add_like.find_brief` 的修复），现可直接 `python3 like_links.py`
- `BRIEF_DIR` 由绝对路径改为动态相对路径，增强可移植性
- 增强文末追加点赞区时的换行与段落格式判断

### validate_brief.py
- 独立加入规则 2 强校验：简报 URL 命中历史去重集合（历史简报/去重文件）直接 FAIL，不再只挂在“不在候选池”分支下漏检
- 校验去重时动态排除当前简报自身，消除误报
- `source_leftovers` 校验收紧：仅对日期未验证条目要求“必须在速览节 + 标注日期未验证”
- 偏好命中标记正则升级为兼容半角/全角括号

### collect_brief.py
- `QUERIES_CN` / `QUERIES_INTL` 改为动态模板：`{month} / {year} / {year_month}` 随当前时间生成（`build_default_queries`），跨月跨年不再硬编码
- 修复 `pick_leftover_item` 中 `month_only` 过窗检查逻辑（month 早于窗口起点所在月即剔）
- 拆解压缩单行语句为规范 Python 格式

### 验证
- 模块导入：4 个脚本全部正常加载
- `validate_brief.py` 校验 2026-09-01 简报：PASS（0 项违规）
- `like_links.py` 及 `--check`：正常识别，退出码 0
- `add_like.py --stats`：正常输出（17 条点赞，偏好方向：科技）
- `collect_brief.py`：正常采集并输出候选

### 备注
- 小g报告中提到的 `dailybrief.py status` 当前不存在（`scripts/` 下无此文件），属报告描述瑕疵，不影响已修脚本

## 2026-09-01 — 执行模式调整（Hermes 配置）

- cron `d0971e033e6b`（今日简报）切换为「Hermes 触发 → 小g(agy) 执行」：采集脚本先行（`collect_brief.py`），LLM 写作 / `validate_brief.py` 校验 / `like_links.py` 点赞由小g完成
- 失败兜底：小g 第一次失败自动重试一次；两次均失败由 Hermes 直接完成生成本期
- 交付方式：`deliver=local + origin`，避免微信限流影响