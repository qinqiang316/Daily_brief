# -*- coding: utf-8 -*-
"""外部 CLI 检索车道（国内、国外检索都走小g=agy CLI）。

生产入口使用 run_lane_for_hermes：小g作者单次检索/补漏，脚本复核可验证证据，
之后由 Hermes 进行语义复核。问题由 Hermes 修正或拒收，不回调小g。
旧 run_lane 保留为兼容测试入口，不由生产管线调用。

可靠性约定：
  - 每个会话独立进程组（start_new_session），超时先 SIGTERM 再 SIGKILL 整个进程组；
  - 每路独立工作目录（批次 lanes/<lane>/），prompt/日志/产物全部落盘；
  - 非零退出、超时、缺产物、产物 JSON 非法、复核未通过 → status=failed + 真实原因，
    不回退历史池、不伪造成功；
  - 总预算由调用方（run_pipeline）按车道并行控制，本模块只对单会话超时负责。
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from modules import executor
from modules import filter as filter_mod  # noqa: E402
from modules.retrieve import wc, fetch_texts_parallel  # noqa: E402
from modules.window import TZ, log  # noqa: E402
import likes as likes_mod  # noqa: E402

KIMI_CLI = os.environ.get("DAILYBRIEF_KIMI", "/Users/qqiang/.kimi-code/bin/kimi")
AGY_CLI = os.environ.get("DAILYBRIEF_AGY", "/Users/qqiang/.local/bin/agy")

MIN_BODY_WORDS = filter_mod.MIN_WORDS  # 车道级正文门槛与统一准入一致（500）
REQUIRED_ITEM_KEYS = (
    "title", "url", "content", "publish_date", "date_source", "date_evidence",
    "date_precision", "direction", "is_explore", "is_preferred",
    "source_key", "source_label",
)

# 车道定义：国内、国外检索都走小g（agy CLI），会话仍互相独立
LANES = {
    "cn": {"agent": "agy", "label": "小g国内检索", "cli": "agy"},
    "intl": {"agent": "agy", "label": "小g国外检索", "cli": "agy"},
}

ITEM_SCHEMA_HINT = {
    "title": "文章标题（字符串）",
    "url": "原文 URL（https，独立文章页，非首页/频道/列表/导航页）",
    "content": "真实抓取的原文正文全文（纯文本，≥500 字；禁止用搜索摘要冒充）",
    "publish_date": "原文发布日期 YYYY-MM-DD（必须有证据，禁止猜测）",
    "date_source": "日期证据来源：json_ld/html_meta/html_time/byline/url/title 之一",
    "date_evidence": "日期证据原文片段（如 meta[datePublished]=2026-10-13 或署名行）",
    "metadata": "真实页面原始metadata对象，json_ld/meta；不可从date_evidence猜造",
    "date_precision": "固定填 day（精确到日；拿不到日精度证据的条目整条不要）",
    "direction": "AI/科技/商业/生活/健康/轨道交通 之一",
    "is_explore": "是否命中探索查询（true/false）",
    "is_preferred": "是否命中偏好查询（true/false）",
    "source_key": "来源标识（如 agent_cn:qbitai）",
    "source_label": "来源展示名（如 量子位）",
}


CANCELLED = threading.Event()
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def cancel_sessions():
    """Only groups started by this process, never global CLI/user processes."""
    CANCELLED.set()
    with _ACTIVE_LOCK:
        for pid in list(_ACTIVE):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(pid, sig)
                except ProcessLookupError: pass


def install_cancel_handlers():
    def cancel(signum, frame):
        cancel_sessions()
        raise KeyboardInterrupt('pipeline cancelled')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, cancel)


def cli_command(kind, prompt, timeout):
    """按 CLI 种类组装命令。agy 全部 flag 必须在 -p 之前（-p 吞后随 flag）。"""
    if kind == "kimi":
        return [KIMI_CLI, "--model", "kimi-code/kimi-for-coding", "--output-format", "text", "-p", prompt]
    if kind == "agy":
        cmd = [AGY_CLI, "--dangerously-skip-permissions", "--output-format", "json",
               "--print-timeout", "%ds" % int(timeout)]
        model = os.environ.get("AGY_MODEL")
        if model:
            cmd += ["--model", model]
        return cmd + ["-p", prompt]
    raise ValueError("未知 CLI 种类: %s" % kind)


def run_cli_session(cmd, cwd, log_path, timeout, env=None):
    """跑一次 CLI 会话：独立进程组、stdout/stderr 落日志、超时杀整个进程组。
    返回退出码；超时返回 124。进程异常启动失败返回 127。"""
    if CANCELLED.is_set():
        return 130
    timeout = float(timeout)
    if timeout <= 0:
        return 124
    with open(log_path, "ab") as fh:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True, env=env)
        except OSError as e:
            fh.write(("[agent_lane] 进程启动失败: %s\n" % e).encode("utf-8"))
            return 127
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc.pid)
        try:
            rc = proc.wait(timeout=timeout)
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # no lingering original children before takeover
            except ProcessLookupError:
                pass
            return rc
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=min(0.1, timeout))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # 父进程已退但后裔忽略 TERM 时兜底
            except ProcessLookupError:
                pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            return 124
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(proc.pid)


def extract_session_id(kind, log_path):
    """从会话日志提取真实会话标识（供复核/审计续接）。取不到返回 None。"""
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            text = f.read()[-20000:]
    except OSError:
        return None
    if kind == "kimi":
        m = re.findall(r"kimi -r (session_[\w-]+)", text)
        if m:
            return m[-1]
        m = re.findall(r"\b(session_[A-Za-z0-9_-]{6,})\b", text)
        return m[-1] if m else None
    if kind == "agy":
        for line in reversed(text.splitlines()):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('conversation_id'):
                return record['conversation_id']
        m = re.findall(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12})\b", text)
        return m[-1] if m else None
    if kind == "hermes":
        return executor.hermes_identity(log_path).get("session_id")
    return None


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_items(items, task):
    """车道产物机械预检（不替代复核，也不替代统一准入 filter/rank）。
    返回 (合格条目列表, 问题描述列表)。窗口/日精度/正文长度/方向/URL 在此先挡一轮，
    让作者在修正轮拿到具体反馈。"""
    window = task.get("window") or {}
    ws, we = window.get("start") or "", window.get("end") or ""
    ok, problems = [], []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            problems.append("条目 %d 不是对象" % (i + 1))
            continue
        missing = [k for k in REQUIRED_ITEM_KEYS if k not in it]
        if missing:
            problems.append("条目 %d 缺字段 %s: %s"
                            % (i + 1, ",".join(missing), str(it.get("url"))[:80]))
            continue
        url = it.get("url") or ""
        if not filter_mod.is_valid_url(url):
            problems.append("非法 URL: %s" % str(url)[:80])
            continue
        if filter_mod.is_aggregate_url(url):
            problems.append("聚合页（首页/频道/列表/导航）不得入选: %s" % url[:80])
            continue
        pub = it.get("publish_date") or ""
        if not filter_mod.is_iso_day(pub) or it.get("date_precision") != "day":
            problems.append("发布日期非日精度或非法: %s" % url[:80])
            continue
        if not (str(it.get("date_source") or "").strip()
                and str(it.get("date_evidence") or "").strip()):
            problems.append("缺 date_source/date_evidence: %s" % url[:80])
            continue
        if ws and we and not (ws <= pub <= we):
            problems.append("发布日期 %s 不在窗口 %s~%s: %s" % (pub, ws, we, url[:80]))
            continue
        if wc(it.get("content") or "") < MIN_BODY_WORDS:
            problems.append("正文不足 %d 字: %s" % (MIN_BODY_WORDS, url[:80]))
            continue
        if it.get("direction") not in likes_mod.DIRECTIONS:
            problems.append("方向非法（须为 %s）: %s"
                            % ("/".join(likes_mod.DIRECTIONS), url[:80]))
            continue
        ok.append(it)
    return ok, problems


def _fmt_queries(queries):
    lines = []
    for i, q in enumerate(queries or [], 1):
        tags = []
        if q.get("preferred"):
            tags.append("偏好")
        if q.get("explore"):
            tags.append("探索")
        tag = ("<" + ",".join(tags) + ">") if tags else ""
        lines.append("%d. [%s]%s %s（最多 %d 条结果）" % (
            i, q.get("direction") or "不限", tag,
            q.get("query"), int(q.get("max_results") or 8)))
    return "\n".join(lines)


def build_author_prompt(task, feedback=None, round_no=1):
    """作者会话 prompt（自足：子 agent 不知道任何上下文）。只写产物文件。"""
    lane = LANES[task["lane"]]
    scope = "国内中文来源" if task["lane"] == "cn" else "国外/英文来源"
    result_path = task["result_file"]
    parts = [
        "你是%s，负责 DailyBrief 每日简报的%s检索（批次 run_id: %s）。"
        % (lane["agent"] == "kimi" and "小k" or "小g", scope, task["run_id"]),
        "",
        "时间窗口：%s ~ %s（上海时区自然日）。只有原文发布日期落在窗口内的文章才可入选。"
        % (task["window"]["start"], task["window"]["end"]),
        "",
        "共享偏好/探索配置：" + json.dumps(task.get("preference") or {}, ensure_ascii=False),
        ("脚本已执行的查询范围（只用于核对主题和偏好，不再逐条搜索）："
         if task.get('seed_file') else "检索查询（逐条执行你的搜索工具，每条查询找 1-3 篇合格文章）："),
        _fmt_queries(task.get("queries")),
        "",
        "每条入选文章必须亲自抓取原文全文并满足（缺一条就整条放弃，禁止凑数）：",
        "1. 真实正文 ≥500 字（搜索摘要/HN 标题/积分不是正文）；"
        "2. 原文发布日期精确到日且有证据（页面 meta/JSON-LD/署名发布时间/URL 日期），"
        "转载日、热议日、正文里提到的事件日期都不是发布日期；"
        "3. 独立文章页，不是首页/频道/列表/导航/翻页页；URL 合法（无反斜杠/空白）；"
        "4. 有作者、有数据有观点的深度内容优先，软文和 AI 水文不要。",
        "",
        "把结果写成 JSON 文件：%s" % result_path,
        "结构：{\"run_id\": \"%s\", \"lane\": \"%s\", \"items\": [...]}，"
        "每个 item 字段：" % (task["run_id"], task["lane"]),
        json.dumps(ITEM_SCHEMA_HINT, ensure_ascii=False, indent=1),
        "",
        "纪律：只写上述 JSON 文件，不改其他任何文件；没有合格文章就如实给空 items，"
        "禁止编造 URL/日期/正文；完成后自己读回该文件确认 JSON 合法。",
    ]
    if task.get('seed_file'):
        parts += [
            "脚本已执行全部查询并抓取真实页面，本批候选与正文：" + task['seed_file'],
            "真实页面快照：" + task['source_snapshot'],
            "先读取上述文件，不重复全网逐站检索。优先审核种子，只保留真实独立正文，最多6篇；先实际写入合格种子结果（无合格则items为空）。",
            "原文content/metadata必须原样复制；日期与hash用modules.filter.extract_date_evidence和hashlib计算，不手抄或猜测。",
            "补查是可选项，总计最多2条定向查询；没有合格种子时允许直接如实缺货，不为凑篇数检索。新增URL必须真实抓取；不要启动后台任务或等待通知。",
            "必须用terminal或文件工具实际写入结果文件，聊天里打印JSON不算完成。",
        ]
    if feedback:
        parts += ["",
                  "上一轮复核未通过（第 %d 轮为修正轮）。逐条处理下面的复核结论："
                  "删除或更换不合格条目并重新核验，然后整体重写结果 JSON。" % round_no,
                  "修正轮只处理反馈，不重跑查询清单；无法及时取得合格替代文就删除问题条目并如实缺货。禁止为替换一条短文重复全网检索。",
                  feedback]
    return "\n".join(parts)


def build_review_prompt(task, round_no):
    """复核会话 prompt：独立会话，重新访问原文逐条核对，产出复核 JSON。"""
    review_path = task["review_file_tmpl"].format(round=round_no)
    return "\n".join([
        "你是独立复核者（第 %d 轮），复核 DailyBrief 批次 run_id=%s 的%s检索产物。"
        % (round_no, task["run_id"],
           "国内" if task["lane"] == "cn" else "国外"),
        "产物文件：%s" % task["result_file"],
        "时间窗口：%s ~ %s。" % (task["window"]["start"], task["window"]["end"]),
        "",
        "共享偏好/探索配置：" + json.dumps(task.get("preference") or {}, ensure_ascii=False),
        "作者本批查询（核is_explore/is_preferred，不可改写）：" + _fmt_queries(task.get("queries")),
        "对产物 items 里每一条，亲自重新访问原文 URL 并核对：",
        "1. URL 可访问、是独立文章页（非首页/频道/列表/导航页）；",
        "2. 页面真实正文 ≥500 字，与产物 content 一致（不是搜索摘要）；"
        "3. 原文发布日期真实存在、精确到日、在窗口内，"
        "产物 date_source/date_evidence 与页面实际证据相符；",
        "4. 标题、方向、is_explore/is_preferred 标记与内容相符。",
        "任何一条不满足即 pass=false 并给出真实原因；不要凭产物自述放行。",
        "",
        "把复核结论写成 JSON 文件：%s" % review_path,
        "结构：{\"run_id\": \"%s\", \"lane\": \"%s\", \"round\": %d, "
        "\"verdict\": \"pass\" 或 \"fail\", "
        "\"items\": [{\"url\": \"...\", \"pass\": true/false, \"reasons\": [\"...\"]}]}"
        % (task["run_id"], task["lane"], round_no),
        "每条复核项还必须带 publish_date、date_evidence（公共extract_date_evidence格式）、content_sha256（真实全文去全部空白后UTF-8 SHA256）。作者条目全量准确覆盖，不允许重复或多余URL。",
        "公共函数从项目根导入modules.filter.extract_date_evidence。content_sha256使用hashlib.sha256(re.sub(r'\\s+', '', content).encode()).hexdigest()实际计算。",
        ("同批脚本抓取快照：" + task['source_snapshot'] + "。先核快照内容与作者全文/日期；独立访问原文核实文章与发布日期，动态导航差异不要求逐字一致。哈希对本批快照正文计算，不对另一个抽取器的输出计算。") if task.get('source_snapshot') else "正文抽取统一用modules.retrieve.fetch_article，日期用modules.filter.extract_date_evidence，不使用不同抽取器计算hash。",
        "必须用terminal或文件工具写入指定JSON文件并读回；只在聊天中打印JSON不算完成。不要委派后台任务，不等待异步通知。",
        "全部条目合格才 verdict=pass；items 为空时若确实没有合格内容，verdict=pass 并在 "
        "items 给空数组。只写该 JSON 文件，不改其他文件。",
    ])


def _lane_failed(task, reason, outcome):
    outcome.update(status="failed", reason=reason)
    log("车道 %s 失败: %s" % (task["lane"], reason))
    return outcome


def run_lane(task, lane_dir, timeout=1200, max_rounds=2, spawn=None, deadline=None, fallback=None):
    """执行一条检索车道：作者 → 独立复核 →（修正 → 再复核，至多 max_rounds 轮）。

    task 字段：lane/run_id/window/queries/result_file/review_file_tmpl。
    返回 outcome dict（status/items/sessions/logs/review/reason），永不抛出 CLI 故障。
    spawn 可注入（测试用），签名同 run_cli_session。
    """
    if fallback is None:
        fallback = spawn is None  # Legacy explicit adapters never invoke inference.
    prepare_seeds = spawn is None and (deadline is None or deadline - time.monotonic() > 30)
    spawn = spawn or run_cli_session
    lane_end = min(deadline or float("inf"), time.monotonic() + float(timeout))
    deadline = lane_end
    def remaining():
        return min(float(timeout), deadline - time.monotonic()) if deadline is not None else float(timeout)
    lane = LANES[task["lane"]]
    kind = lane["cli"]
    os.makedirs(lane_dir, exist_ok=True)
    outcome = {
        "lane": task["lane"], "agent": lane["agent"], "label": lane["label"],
        "status": "failed", "reason": None, "items": [], "rounds": 0,
        "sessions": {"author": [], "reviewer": []}, "logs": [],
        "review": {"verdict": None, "rounds": []},
        "attempts": [], "started_at": datetime.now(TZ).isoformat(),
    }

    if prepare_seeds:
        from modules import lane_seeds, run_store
        task = dict(task)
        log('车道 %s 脚本检索与原文快照开始' % task['lane'])
        seed_budget = run_store.DeadlineBudget(min(lane_end, time.monotonic() + 240))
        seed, snapshot, digest = lane_seeds.collect(task, seed_budget)
        task.update(seed_file=seed, source_snapshot=snapshot,
                    source_snapshot_sha256=digest)
        log('车道 %s 原文快照就绪，开始独立作者/复核会话' % task['lane'])

    feedback = None
    for round_no in range(1, max_rounds + 1):
        outcome["rounds"] = round_no
        # ── 作者会话 ──
        prompt = build_author_prompt(task, feedback=feedback, round_no=round_no)
        prompt_path = os.path.join(lane_dir, "author_prompt_r%d.txt" % round_no)
        log_path = os.path.join(lane_dir, "author_r%d.log" % round_no)
        _write_text(prompt_path, prompt)
        if os.path.exists(task["result_file"]):
            os.unlink(task["result_file"])  # A previous round is never fresh author output.
        outcome["logs"].append(log_path)
        if remaining() <= 0:
            return _lane_failed(task, "pipeline deadline exceeded", outcome)
        # Reserve only this round's review. Pre-splitting the unused correction
        # round cut a live search at 300s of a 1200s lane.
        step_timeout = min(remaining(), max(0.1, remaining() * 2 / 3))
        rc, execution = executor.execute(kind, cli_command(kind, prompt, step_timeout),
            prompt, lane_dir, log_path, task["result_file"], step_timeout, spawn,
            fallback=fallback, role="%s:author:r%d" % (task["lane"], round_no))
        outcome["attempts"].append(execution)
        sid = extract_session_id(execution["executor"], execution["log"])
        outcome["sessions"]["author"].append(dict(execution, round=round_no, rc=rc, session=sid))
        if rc != 0:
            return _lane_failed(task, "作者会话异常退出 rc=%s（日志 %s）" % (rc, log_path),
                                outcome)
        if not os.path.isfile(task["result_file"]):
            return _lane_failed(task, "作者会话结束但未产出结果文件 %s"
                                % task["result_file"], outcome)
        try:
            result = _read_json(task["result_file"])
        except (ValueError, OSError) as e:
            return _lane_failed(task, "结果 JSON 无法解析: %s" % e, outcome)
        if result.get("lane") != task["lane"]:
            return _lane_failed(task, "结果 lane 错配", outcome)
        if result.get("run_id") != task["run_id"]:
            return _lane_failed(task, "结果 run_id 错配（%s ≠ %s），拒绝采用"
                                % (result.get("run_id"), task["run_id"]), outcome)
        items = result.get("items")
        if not isinstance(items, list):
            return _lane_failed(task, "结果 items 不是数组", outcome)
        ok_items, problems = check_items(items, task)
        _write_text(os.path.join(lane_dir, "author_result_r%d.json" % round_no), json.dumps(result, ensure_ascii=False))

        # ── 独立复核会话（新 CLI 会话，不续接作者）──
        rprompt = build_review_prompt(task, round_no)
        rprompt_path = os.path.join(lane_dir, "review_prompt_r%d.txt" % round_no)
        rlog_path = os.path.join(lane_dir, "review_r%d.log" % round_no)
        _write_text(rprompt_path, rprompt)
        outcome["logs"].append(rlog_path)
        review_file = task["review_file_tmpl"].format(round=round_no)
        if os.path.exists(review_file):
            os.unlink(review_file)
        if remaining() <= 0:
            return _lane_failed(task, "pipeline deadline exceeded", outcome)
        step_timeout = remaining()
        rc, execution = executor.execute(kind, cli_command(kind, rprompt, step_timeout),
            rprompt, lane_dir, rlog_path, review_file, step_timeout, spawn,
            fallback=fallback, role="%s:reviewer:r%d" % (task["lane"], round_no))
        outcome["attempts"].append(execution)
        rsid = extract_session_id(execution["executor"], execution["log"])
        outcome["sessions"]["reviewer"].append(dict(execution, round=round_no, rc=rc, session=rsid))
        if rc != 0:
            return _lane_failed(task, "复核会话异常退出 rc=%s（日志 %s）" % (rc, rlog_path),
                                outcome)
        if not os.path.isfile(review_file):
            return _lane_failed(task, "复核会话结束但未产出复核文件 %s" % review_file,
                                outcome)
        try:
            review = _read_json(review_file)
        except (ValueError, OSError) as e:
            return _lane_failed(task, "复核 JSON 无法解析: %s" % e, outcome)
        if review.get("run_id") != task["run_id"]:
            return _lane_failed(task, "复核 run_id 错配，拒绝采用", outcome)
        if review.get("lane") != task["lane"] or review.get("round") != round_no:
            return _lane_failed(task, "复核 lane/round 错配", outcome)
        all_sids = [s.get("session") for role in ("author", "reviewer") for s in outcome["sessions"][role]]
        if not sid or not rsid or len(all_sids) != len(set(all_sids)):
            return _lane_failed(task, "缺真实独立会话标识或会话重复", outcome)
        verdicts = review.get("items")
        if review.get("verdict") not in ("pass", "fail") or not isinstance(verdicts, list):
            return _lane_failed(task, "复核结论结构非法（缺 verdict/items）", outcome)
        outcome["review"]["rounds"].append({
            "round": round_no, "verdict": review["verdict"],
            "items": verdicts, "session": rsid, "file": review_file,
        })
        outcome["review"]["verdict"] = review["verdict"]

        # 复核未过 / 机械预检问题 → 组织反馈进入修正轮
        author_urls = [it.get("url") for it in items if isinstance(it, dict)]
        review_urls = [v.get("url") for v in verdicts if isinstance(v, dict)]
        coverage = (len(author_urls) == len(items) == len(set(author_urls))
                    and len(review_urls) == len(verdicts) == len(set(review_urls))
                    and set(author_urls) == set(review_urls))
        all_pass = all(isinstance(v, dict) and v.get("pass") is True for v in verdicts)
        if not coverage or not all_pass:
            problems.append("复核必须准确覆盖全部作者URL，无重复/多余，pass必须为布尔true")
        if review["verdict"] == "pass" and not problems:
            # Supervisor fetch is evidence, not reviewer prose. No metadata invented from date_evidence.
            from modules.run_store import DeadlineBudget
            originals = {}
            if task.get('source_snapshot'):
                with open(task['source_snapshot'], 'rb') as f:
                    raw_snapshot = f.read()
                if hashlib.sha256(raw_snapshot).hexdigest() != task['source_snapshot_sha256']:
                    return _lane_failed(task, '脚本原文快照被修改', outcome)
                originals = json.loads(raw_snapshot)
            missing_urls = [url for url in author_urls if url not in originals]
            if missing_urls:
                originals.update(fetch_texts_parallel(missing_urls, timeout=12,
                    budget=DeadlineBudget(deadline) if deadline is not None else None,
                    with_metadata=True))
            evidence = {}
            for it in ok_items:
                original = originals.get(it["url"])
                if not isinstance(original, dict):
                    problems.append("原文复核抓取失败: " + it["url"])
                    continue
                text = original.get("text") or ""
                metadata = original.get("metadata") or {}
                ev = filter_mod.extract_date_evidence(dict(it, content=text, metadata=metadata))
                digest = hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()
                v = next(v for v in verdicts if v["url"] == it["url"])
                if (wc(text) < MIN_BODY_WORDS or ev["precision"] != "day"
                        or ev["publish_date"] != it["publish_date"]
                        or v.get("content_sha256") != digest
                        or v.get("publish_date") != ev["publish_date"]
                        or v.get("date_evidence") != ev["evidence"]):
                    problems.append("独立原文/日期/哈希复核证据不符: " + it["url"])
                    continue
                if re.sub(r"\s+", "", it["content"]) != re.sub(r"\s+", "", text):
                    problems.append("作者正文与真实原文不一致: " + it["url"])
                    continue
                from modules.region import bind_lane
                bind_lane(it, task["lane"])
                it["metadata"] = metadata
                it["source_verification"] = {"content_sha256": digest, "date": ev}
                evidence[it["url"]] = original
            _write_text(os.path.join(lane_dir, "source_evidence_r%d.json" % round_no),
                        json.dumps(evidence, ensure_ascii=False))
        if review["verdict"] == "pass" and not problems:
            outcome.update(status="ok", items=ok_items, reason=None)
            outcome["finished_at"] = datetime.now(TZ).isoformat()
            log("车道 %s 通过：%d 条候选（%d 轮复核）"
                % (task["lane"], len(ok_items), round_no))
            return outcome
        fb = ["复核者逐条结论：", json.dumps(verdicts, ensure_ascii=False, indent=1)]
        if problems:
            fb.append("脚本预检问题（这些条目必须修复或删除）：")
            fb.extend("- " + p for p in problems)
        feedback = "\n".join(fb)

    return _lane_failed(task, "%d 轮修正后复核仍未通过" % max_rounds, outcome)


def run_lane_for_hermes(task, lane_dir, timeout=1200, spawn=None,
                        deadline=None, fallback=None):
    """一次小g作者会话 + 脚本证据核验；语义复核留给 Hermes。

    无论条目被拒多少，都不重跑作者。真实CLI传输/超时错误仍由 executor
    按现有策略处理；语义问题在 pipeline_job ready 后由 Hermes 处置。
    """
    if fallback is None:
        fallback = spawn is None
    prepare_seeds = spawn is None and (deadline is None or deadline - time.monotonic() > 30)
    spawn = spawn or run_cli_session
    lane_end = min(deadline or float("inf"), time.monotonic() + float(timeout))
    lane = LANES[task["lane"]]
    os.makedirs(lane_dir, exist_ok=True)
    outcome = {
        "lane": task["lane"], "agent": lane["agent"], "label": lane["label"],
        "status": "failed", "reason": None, "items": [], "rounds": 1,
        "sessions": {"author": [], "reviewer": []}, "logs": [], "attempts": [],
        "review": {"verdict": "pending_hermes", "rounds": []},
        "semantic_review": {"status": "pending", "reviewer": "Hermes"},
        "mechanical_rejections": [], "started_at": datetime.now(TZ).isoformat(),
    }
    if prepare_seeds:
        from modules import lane_seeds, run_store
        task = dict(task)
        log("车道 %s 脚本检索与原文快照开始" % task["lane"])
        seed_budget = run_store.DeadlineBudget(min(lane_end, time.monotonic() + 240))
        seed, snapshot, digest = lane_seeds.collect(task, seed_budget)
        task.update(seed_file=seed, source_snapshot=snapshot,
                    source_snapshot_sha256=digest)
        log("车道 %s 原文快照就绪，开始小g选文与定向补漏" % task["lane"])

    prompt = build_author_prompt(task)
    prompt_path = os.path.join(lane_dir, "author_prompt.txt")
    log_path = os.path.join(lane_dir, "author.log")
    _write_text(prompt_path, prompt)
    result_path = task["result_file"]
    if os.path.exists(result_path):
        os.unlink(result_path)
    outcome["logs"].append(log_path)
    remaining = lane_end - time.monotonic()
    if remaining <= 0:
        return _lane_failed(task, "pipeline deadline exceeded before author session", outcome)
    rc, execution = executor.execute(kind=lane["cli"],
        command=cli_command(lane["cli"], prompt, remaining), prompt=prompt,
        cwd=lane_dir, log=log_path, artifact=result_path, timeout=remaining,
        spawn=spawn, fallback=fallback, role="%s:author" % task["lane"])
    outcome["attempts"].append(execution)
    sid = extract_session_id(execution["executor"], execution["log"])
    outcome["sessions"]["author"].append(dict(execution, rc=rc, session=sid))
    if rc != 0:
        return _lane_failed(task, "作者会话异常退出 rc=%s（日志 %s）" % (rc, log_path), outcome)
    if not sid:
        return _lane_failed(task, "作者会话缺少真实session标识", outcome)
    if not os.path.isfile(result_path):
        return _lane_failed(task, "作者会话结束但未产出结果文件 %s" % result_path, outcome)
    try:
        result = _read_json(result_path)
    except (ValueError, OSError) as exc:
        return _lane_failed(task, "结果 JSON 无法解析: %s" % exc, outcome)
    if result.get("lane") != task["lane"] or result.get("run_id") != task["run_id"]:
        return _lane_failed(task, "结果 lane/run_id 错配，拒绝采用", outcome)
    items = result.get("items")
    if not isinstance(items, list):
        return _lane_failed(task, "结果 items 不是数组", outcome)
    _write_text(os.path.join(lane_dir, "author_result_r1.json"),
                json.dumps(result, ensure_ascii=False))
    ok_items, problems = check_items(items, task)
    ok_urls = {it.get("url") for it in ok_items}
    outcome["mechanical_rejections"].extend(
        {"url": it.get("url"), "reason": "脚本机械预检未通过"}
        for it in items if isinstance(it, dict) and it.get("url") not in ok_urls)
    outcome["mechanical_rejections"].extend(
        {"url": None, "reason": problem} for problem in problems
        if not any(isinstance(it, dict) and it.get("url") and it["url"] in problem
                   for it in items))

    originals = {}
    if task.get("source_snapshot"):
        try:
            snapshot_bytes = Path(task["source_snapshot"]).read_bytes()
            if hashlib.sha256(snapshot_bytes).hexdigest() != task.get("source_snapshot_sha256"):
                return _lane_failed(task, "脚本原文快照哈希不符", outcome)
            originals = json.loads(snapshot_bytes)
        except (OSError, ValueError) as exc:
            return _lane_failed(task, "脚本原文快照读取失败: %s" % exc, outcome)
    missing = [it["url"] for it in ok_items if it["url"] not in originals]
    if missing:
        from modules.run_store import DeadlineBudget
        originals.update(fetch_texts_parallel(missing, timeout=12,
            budget=DeadlineBudget(lane_end), with_metadata=True))
    accepted, evidence = [], {}
    for item in ok_items:
        original = originals.get(item["url"])
        reason = None
        if not isinstance(original, dict):
            reason = "真实原文抓取失败"
        else:
            text = original.get("text") or ""
            metadata = original.get("metadata") or {}
            date = filter_mod.extract_date_evidence(dict(item, content=text, metadata=metadata))
            digest = hashlib.sha256(re.sub(r"\s+", "", text).encode()).hexdigest()
            if wc(text) < MIN_BODY_WORDS:
                reason = "真实正文不足500字"
            elif date["precision"] != "day" or date["publish_date"] != item["publish_date"]:
                reason = "原文发布日期证据不符"
            elif re.sub(r"\s+", "", item["content"]) != re.sub(r"\s+", "", text):
                reason = "作者正文与真实原文不一致"
            else:
                from modules.region import bind_lane
                bind_lane(item, task["lane"])
                item["metadata"] = metadata
                item["source_verification"] = {"content_sha256": digest, "date": date}
                accepted.append(item)
                evidence[item["url"]] = original
        if reason:
            outcome["mechanical_rejections"].append({"url": item.get("url"), "reason": reason})

    _write_text(os.path.join(lane_dir, "source_evidence.json"),
                json.dumps(evidence, ensure_ascii=False))
    _write_text(os.path.join(lane_dir, "accepted_items.json"),
                json.dumps({"run_id": task["run_id"], "lane": task["lane"],
                            "items": accepted}, ensure_ascii=False))
    _write_text(os.path.join(lane_dir, "mechanical_review.json"),
                json.dumps({"run_id": task["run_id"], "lane": task["lane"],
                            "status": "complete", "rejections": outcome["mechanical_rejections"]},
                           ensure_ascii=False, indent=2))
    outcome.update(status="ok", items=accepted, reason=None,
                   finished_at=datetime.now(TZ).isoformat())
    log("车道 %s 脚本证据检查完成：保留%d条、拒收%d条；待Hermes语义复核"
        % (task["lane"], len(accepted), len(outcome["mechanical_rejections"])))
    return outcome
