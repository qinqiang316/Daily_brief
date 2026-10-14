# -*- coding: utf-8 -*-
"""外部 CLI 检索车道（小k=Kimi CLI 国内检索 / 小g=agy CLI 国外检索）。

每个车道 = 作者会话 + 独立复核会话：两者都是独立进程、独立 CLI 会话
（复核会话不续接作者会话），作者产出 JSON → 复核者逐条复核 → 不合格则作者
修正 → 复核者再审，全部通过才返回候选。仅写一个 reviewer 字段不算复核证据。

可靠性约定：
  - 每个会话独立进程组（start_new_session），超时先 SIGTERM 再 SIGKILL 整个进程组；
  - 每路独立工作目录（批次 lanes/<lane>/），prompt/日志/产物全部落盘；
  - 非零退出、超时、缺产物、产物 JSON 非法、复核未通过 → status=failed + 真实原因，
    不回退历史池、不伪造成功；
  - 总预算由调用方（run_pipeline）按车道并行控制，本模块只对单会话超时负责。
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from modules import filter as filter_mod  # noqa: E402
from modules.retrieve import wc  # noqa: E402
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

# 车道定义：国内=小k（kimi CLI），国外=小g（agy CLI）
LANES = {
    "cn": {"agent": "kimi", "label": "小k国内检索", "cli": "kimi"},
    "intl": {"agent": "agy", "label": "小g国外检索", "cli": "agy"},
}

ITEM_SCHEMA_HINT = {
    "title": "文章标题（字符串）",
    "url": "原文 URL（https，独立文章页，非首页/频道/列表/导航页）",
    "content": "真实抓取的原文正文全文（纯文本，≥500 字；禁止用搜索摘要冒充）",
    "publish_date": "原文发布日期 YYYY-MM-DD（必须有证据，禁止猜测）",
    "date_source": "日期证据来源：json_ld/html_meta/html_time/byline/url/title 之一",
    "date_evidence": "日期证据原文片段（如 meta[datePublished]=2026-10-13 或署名行）",
    "date_precision": "固定填 day（精确到日；拿不到日精度证据的条目整条不要）",
    "direction": "AI/科技/商业/生活/健康/轨道交通 之一",
    "is_explore": "是否命中探索查询（true/false）",
    "is_preferred": "是否命中偏好查询（true/false）",
    "source_key": "来源标识（如 agent_cn:qbitai）",
    "source_label": "来源展示名（如 量子位）",
}


def cli_command(kind, prompt, timeout):
    """按 CLI 种类组装命令。agy 全部 flag 必须在 -p 之前（-p 吞后随 flag）。"""
    if kind == "kimi":
        return [KIMI_CLI, "--output-format", "text", "-p", prompt]
    if kind == "agy":
        cmd = [AGY_CLI, "--dangerously-skip-permissions",
               "--print-timeout", "%ds" % int(timeout)]
        model = os.environ.get("AGY_MODEL")
        if model:
            cmd += ["--model", model]
        return cmd + ["-p", prompt]
    raise ValueError("未知 CLI 种类: %s" % kind)


def run_cli_session(cmd, cwd, log_path, timeout, env=None):
    """跑一次 CLI 会话：独立进程组、stdout/stderr 落日志、超时杀整个进程组。
    返回退出码；超时返回 124。进程异常启动失败返回 127。"""
    timeout = max(1, int(timeout))
    with open(log_path, "ab") as fh:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True, env=env)
        except OSError as e:
            fh.write(("[agent_lane] 进程启动失败: %s\n" % e).encode("utf-8"))
            return 127
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
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
            return 124


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
        m = re.findall(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12})\b", text)
        return m[-1] if m else None
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
        "检索查询（逐条执行你的搜索工具，每条查询找 1-3 篇合格文章）：",
        _fmt_queries(task.get("queries")),
        "",
        "每条入选文章必须亲自抓取原文全文并满足（缺一条就整条放弃，禁止凑数）：",
        "1. 真实正文 ≥%d 字（搜索摘要/HN 标题/积分不是正文）；"
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
    if feedback:
        parts += ["",
                  "上一轮复核未通过（第 %d 轮为修正轮）。逐条处理下面的复核结论："
                  "删除或更换不合格条目并重新核验，然后整体重写结果 JSON。" % round_no,
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
        "对产物 items 里每一条，亲自重新访问原文 URL 并核对：",
        "1. URL 可访问、是独立文章页（非首页/频道/列表/导航页）；",
        "2. 页面真实正文 ≥%d 字，与产物 content 一致（不是搜索摘要）；"
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
        "全部条目合格才 verdict=pass；items 为空时若确实没有合格内容，verdict=pass 并在 "
        "items 给空数组。只写该 JSON 文件，不改其他文件。",
    ])


def _lane_failed(task, reason, outcome):
    outcome.update(status="failed", reason=reason)
    log("车道 %s 失败: %s" % (task["lane"], reason))
    return outcome


def run_lane(task, lane_dir, timeout=1200, max_rounds=2, spawn=None):
    """执行一条检索车道：作者 → 独立复核 →（修正 → 再复核，至多 max_rounds 轮）。

    task 字段：lane/run_id/window/queries/result_file/review_file_tmpl。
    返回 outcome dict（status/items/sessions/logs/review/reason），永不抛出 CLI 故障。
    spawn 可注入（测试用），签名同 run_cli_session。
    """
    spawn = spawn or run_cli_session
    lane = LANES[task["lane"]]
    kind = lane["cli"]
    os.makedirs(lane_dir, exist_ok=True)
    outcome = {
        "lane": task["lane"], "agent": lane["agent"], "label": lane["label"],
        "status": "failed", "reason": None, "items": [], "rounds": 0,
        "sessions": {"author": [], "reviewer": []}, "logs": [],
        "review": {"verdict": None, "rounds": []},
        "started_at": datetime.now(TZ).isoformat(),
    }

    feedback = None
    for round_no in range(1, max_rounds + 1):
        outcome["rounds"] = round_no
        # ── 作者会话 ──
        prompt = build_author_prompt(task, feedback=feedback, round_no=round_no)
        prompt_path = os.path.join(lane_dir, "author_prompt_r%d.txt" % round_no)
        log_path = os.path.join(lane_dir, "author_r%d.log" % round_no)
        _write_text(prompt_path, prompt)
        outcome["logs"].append(log_path)
        rc = spawn(cli_command(kind, prompt, timeout), lane_dir, log_path, timeout)
        sid = extract_session_id(kind, log_path)
        outcome["sessions"]["author"].append({"round": round_no, "rc": rc,
                                              "session": sid, "log": log_path})
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
        if result.get("run_id") != task["run_id"]:
            return _lane_failed(task, "结果 run_id 错配（%s ≠ %s），拒绝采用"
                                % (result.get("run_id"), task["run_id"]), outcome)
        items = result.get("items")
        if not isinstance(items, list):
            return _lane_failed(task, "结果 items 不是数组", outcome)
        ok_items, problems = check_items(items, task)

        # ── 独立复核会话（新 CLI 会话，不续接作者）──
        rprompt = build_review_prompt(task, round_no)
        rprompt_path = os.path.join(lane_dir, "review_prompt_r%d.txt" % round_no)
        rlog_path = os.path.join(lane_dir, "review_r%d.log" % round_no)
        _write_text(rprompt_path, rprompt)
        outcome["logs"].append(rlog_path)
        review_file = task["review_file_tmpl"].format(round=round_no)
        rc = spawn(cli_command(kind, rprompt, timeout), lane_dir, rlog_path, timeout)
        rsid = extract_session_id(kind, rlog_path)
        outcome["sessions"]["reviewer"].append({"round": round_no, "rc": rc,
                                                "session": rsid, "log": rlog_path})
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
        verdicts = review.get("items")
        if review.get("verdict") not in ("pass", "fail") or not isinstance(verdicts, list):
            return _lane_failed(task, "复核结论结构非法（缺 verdict/items）", outcome)
        outcome["review"]["rounds"].append({
            "round": round_no, "verdict": review["verdict"],
            "items": verdicts, "session": rsid, "file": review_file,
        })
        outcome["review"]["verdict"] = review["verdict"]

        # 复核未过 / 机械预检问题 → 组织反馈进入修正轮
        failed_urls = {v.get("url") for v in verdicts
                       if isinstance(v, dict) and v.get("pass") is False}
        if review["verdict"] == "pass" and not failed_urls and not problems:
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
