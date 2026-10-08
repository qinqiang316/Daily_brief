import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .window import TZ, log

CLI = "/Users/qqiang/.hermes/hermes-agent/venv/bin/python"
CLI_SCRIPT = "/Users/qqiang/.hermes/skills/anysearch/scripts/anysearch_cli.py"
TIMEOUT = 60
FETCH_WORKERS = 4        # 正文抓取有界并发上限
ACS_TIMEOUT = 90         # ACS 子进程单次上限（原 180，避免累积超时）
ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"
ANYSEARCH_ENV_CANDIDATES = [
    "/Users/qqiang/.hermes/skills/anysearch/scripts/.env",
    "/Users/qqiang/.hermes/skills/anysearch/.env",
]


class Budget:
    """采集总预算：所有网络阶段共享一个截止时间，防止累积超时。"""

    def __init__(self, seconds):
        self.deadline = time.time() + max(0.0, float(seconds))

    def remaining(self):
        return max(0.0, self.deadline - time.time())

    def expired(self):
        return self.remaining() <= 0.0

    def timeout(self, cap):
        """单次调用实际超时 = min(cap, 剩余预算)，至少 1 秒。"""
        return max(1, int(min(cap, self.remaining())))

    def derive(self, seconds):
        """派生子预算：deadline = min(自身 deadline, now+seconds)。
        用于给后续阶段预留预算（如搜索阶段给正文抓取预留）。"""
        sub = Budget(0)
        sub.deadline = min(self.deadline, time.time() + max(0.0, float(seconds)))
        return sub


def run_cli(args, timeout=None, budget=None):
    eff = budget.timeout(timeout or TIMEOUT) if budget else (timeout or TIMEOUT)
    if budget and budget.expired():
        log("CLI 跳过（总预算耗尽）: %s" % " ".join(args)[:120])
        return None
    try:
        p = subprocess.run([CLI, CLI_SCRIPT] + args, capture_output=True,
                           text=True, timeout=eff)
        if p.returncode == 0:
            return p.stdout
        log("CLI 失败(%d): %s" % (p.returncode, p.stderr[:200]))
    except subprocess.TimeoutExpired:
        log("CLI 超时(%ss): %s" % (eff, " ".join(args)[:120]))
    except Exception as e:
        log("CLI 异常: %s" % e)
    return None

def parse_search_markdown(md):
    items = []
    if not md:
        return items
    chunks = re.split(r"\n(?=## Query \d+:)", "\n" + md.strip())
    fallback_qi = 0
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        qm = re.match(r"## Query (\d+):", chunk)
        qi, body = (int(qm.group(1)) - 1, chunk[qm.end():]) if qm else (None, chunk)
        found = 0
        for block in re.split(r"\n### (?=\d+\. )", "\n" + body):
            m = re.match(r"(\d+)\.\s+(.+?)\s*\n- \*\*URL\*\*:\s*(\S+)", block)
            if not m:
                continue
            bbody = re.split(r"\n## Query \d+:", block[m.end():])[0]
            bbody = re.sub(r"\n### \d+\..*", "", bbody)
            items.append({"title": m.group(2).strip(), "url": m.group(3).strip(),
                          "content": bbody.strip(),
                          "query_idx": qi if qi is not None else fallback_qi})
            found += 1
        if not qm and found:
            fallback_qi += 1
    return items

def _get_anysearch_api_key():
    api_key = os.environ.get("ANYSEARCH_API_KEY", "")
    if not api_key:
        for env_path in ANYSEARCH_ENV_CANDIDATES:
            if os.path.isfile(env_path):
                try:
                    with open(env_path, encoding="utf-8-sig") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("ANYSEARCH_API_KEY="):
                                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
                except Exception:
                    pass
            if api_key:
                break
    return api_key

def anysearch_http_batch(queries, budget=None):
    api_key = _get_anysearch_api_key()
    if not api_key:
        log("anysearch HTTP 兜底：找不到 API key")
        return None
    if budget and budget.expired():
        log("anysearch HTTP 兜底跳过（总预算耗尽）")
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "batch_search", "arguments": {"queries": queries}}}
    try:
        req = urllib.request.Request(ANYSEARCH_ENDPOINT, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + api_key}, method="POST")
        with urllib.request.urlopen(req, timeout=budget.timeout(30) if budget else 30) as r:
            items = parse_search_markdown(r.read().decode("utf-8", errors="replace"))
        if items:
            log("anysearch HTTP 兜底成功: %d 条" % len(items))
        return items or None
    except Exception as e:
        log("anysearch HTTP 兜底失败: %s" % e)
        return None

def parse_extract_markdown(raw):
    """解析 extract 返回的原始文本/JSON，提取其中的 [标题](url) 链接列表。"""
    if not raw:
        return []
    content = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if "result" in data and isinstance(data["result"], dict):
                    res_content = data["result"].get("content", [])
                    for c in res_content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            try:
                                inner = json.loads(c.get("text", ""))
                                if isinstance(inner, dict) and "content" in inner:
                                    content = inner["content"]
                                    break
                            except Exception:
                                content = c.get("text", "")
                                break
                elif "content" in data:
                    content = data["content"]
        except Exception:
            content = raw

    items = []
    if isinstance(content, str):
        for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", content):
            t = re.sub(r"\s+", " ", title).strip()
            u = url.strip()
            if t and u:
                items.append({"title": t, "url": u})
    return items

def anysearch_http_extract(url, budget=None):
    """通过 HTTP JSON-RPC tools/call 调用 anysearch extract。"""
    api_key = _get_anysearch_api_key()
    if not api_key:
        log("anysearch HTTP extract 兜底：找不到 API key")
        return None
    if budget and budget.expired():
        log("anysearch HTTP extract 跳过（总预算耗尽）")
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "extract", "arguments": {"url": url}}}
    try:
        req = urllib.request.Request(ANYSEARCH_ENDPOINT, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + api_key}, method="POST")
        with urllib.request.urlopen(req, timeout=budget.timeout(30) if budget else 30) as r:
            items = parse_extract_markdown(r.read().decode("utf-8", errors="replace"))
        if items:
            log("anysearch HTTP extract 成功: %d 条" % len(items))
        return items or None
    except Exception as e:
        log("anysearch HTTP extract 失败: %s" % e)
        return None

def anysearch_extract(url, budget=None):
    """优先 CLI，失败走 HTTP 兜底调用 anysearch extract，返回解析后的条目列表 [{"title": ..., "url": ...}]。
    接入总预算：预算耗尽直接返回 []，不做任何网络请求。"""
    if budget and budget.expired():
        log("anysearch extract 跳过（总预算耗尽）: %s" % url[:80])
        return []
    out = run_cli(["extract", "--url", url], budget=budget)
    items = parse_extract_markdown(out) if out else None
    if not items:
        items = anysearch_http_extract(url, budget=budget)
    return items or []

FETCH_TEXT_LIMIT = 6000  # 英文 500 词 ≈ 3000+ 字符，原 2500 会把合格英文正文截到准入线以下

_JSONLD_RE = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)
_META_TAG_RE = re.compile(r"<meta[^>]+>", re.I)
_META_NAME_RE = re.compile(r"(?:name|property|itemprop)=[\"']([^\"']+)[\"']", re.I)
_META_CONTENT_RE = re.compile(r"content=[\"']([^\"']*)[\"']", re.I)
_TIME_TAG_RE = re.compile(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", re.I)


def _walk_jsonld_dates(node, out):
    """递归收集 JSON-LD 里的 datePublished/dateCreated（@graph/数组/嵌套 NewsArticle）。"""
    if isinstance(node, dict):
        for k in ("datePublished", "dateCreated"):
            v = node.get(k)
            if isinstance(v, str) and k not in out:
                out[k] = v
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_jsonld_dates(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld_dates(v, out)


def extract_html_metadata(raw_html):
    """在 HTML 清洗之前提取发布元数据：JSON-LD + meta 标签 + <time datetime>。
    返回 {"json_ld": {...}, "meta": {name/property: content}, "time": [datetime, ...]}。"""
    json_ld = {}
    for m in _JSONLD_RE.finditer(raw_html):
        try:
            _walk_jsonld_dates(json.loads(m.group(1)), json_ld)
        except Exception:
            continue
    meta = {}
    for m in _META_TAG_RE.finditer(raw_html):
        tag = m.group(0)
        nm, ct = _META_NAME_RE.search(tag), _META_CONTENT_RE.search(tag)
        if nm and ct and ct.group(1).strip():
            meta[nm.group(1)] = ct.group(1).strip()
    times = [m.group(1).strip() for m in _TIME_TAG_RE.finditer(raw_html)]
    return {"json_ld": json_ld, "meta": meta, "time": times}


def fetch_article(url, timeout=15, limit=FETCH_TEXT_LIMIT):
    """结构化正文抓取：返回 {"text": 正文, "metadata": {"json_ld":..., "meta":...}}。
    metadata 在 HTML 清洗前提取（datePublished / JSON-LD 不会被丢掉）。
    正文过短（<200 字符）或抓取失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(500000).decode("utf-8", errors="ignore")
        metadata = extract_html_metadata(raw)
        raw = re.sub(r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", re.sub(r"&[a-z]+;|<[^>]+>", " ", raw)).strip()
        if len(text) < 200:
            return None
        return {"text": text[:limit], "metadata": metadata}
    except Exception as e:
        log("预抓 %s 失败: %s" % (url[:60], e))
        return None


def fetch_article_text(url, timeout=15, limit=FETCH_TEXT_LIMIT):
    """兼容接口：只返回正文字符串。需要日期元数据请用 fetch_article。"""
    art = fetch_article(url, timeout, limit)
    return art["text"] if art else None


def fetch_texts_parallel(urls, timeout=15, limit=FETCH_TEXT_LIMIT, budget=None,
                         max_workers=FETCH_WORKERS, with_metadata=False):
    """有界并发批量抓正文，单 URL 故障隔离。
    默认返回 {url: text|None}（兼容旧调用）；
    with_metadata=True 时返回 {url: {"text":..., "metadata":...}|None}。
    总预算耗尽即停止派发新任务，避免累积超时。"""
    fetcher = fetch_article if with_metadata else fetch_article_text
    results = {}
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return results
    pending = list(urls)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {}
        for u in pending:
            if budget and budget.expired():
                log("正文抓取提前停止（总预算耗尽），剩余 %d 个 URL 未抓" % (len(pending) - len(futs)))
                break
            eff = budget.timeout(timeout) if budget else timeout
            futs[ex.submit(fetcher, u, eff, limit)] = u
        for fut, u in futs.items():
            if u in results:
                continue
            wait = budget.remaining() if budget else None
            try:
                results[u] = fut.result(timeout=max(1.0, wait) if wait is not None else None)
            except Exception as e:
                log("正文抓取故障隔离: %s (%s)" % (u[:60], e))
                results[u] = None
    return results


def fetch_hn(now, budget=None):
    items = []
    if budget and budget.expired():
        log("HN 采集跳过（总预算耗尽）")
        return items
    since = int((now - timedelta(hours=72)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search?tags=story"
           "&numericFilters=created_at_i%%3E%d,points%%3E=20&hitsPerPage=40" % since)
    try:
        with urllib.request.urlopen(url, timeout=budget.timeout(25) if budget else 25) as r:
            data = json.loads(r.read().decode("utf-8"))
        for hit in data.get("hits", []):
            pts, created = hit.get("points") or 0, hit.get("created_at") or ""
            discussed_at = ""
            if created:
                try:
                    discussed_at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d")
                except Exception:
                    discussed_at = created[:10]
            # 加固：created_at 是 HN 热议日期，不是文章发布日期。
            # 发布日期一律留空，由 filter 经 extract_publish_date 从文章自身 URL/正文取证据；
            # 旧文新热议 / 无精确日期证据的 HN 条目不得入推荐池。
            items.append({"title": hit.get("title", ""), "url": hit.get("url") or ("https://news.ycombinator.com/item?id=" + str(hit.get("objectID", ""))), "content": "HN 热议 %d 分。%s" % (pts, hit.get("title", "")), "publish_date": "", "date_verified": False, "hn_points": pts, "hn_discussed_at": discussed_at})
        items.sort(key=lambda x: x.get("hn_points") or 0, reverse=True)
        log("HN 拉到 %d 条(≥20分,按分排序)" % len(items))
    except Exception as e:
        log("HN API 失败: %s" % e)
    return items

def fetch_telegram_channels(channels, max_per_channel=15, budget=None):
    """从 Telegram 公开频道预览页抓取消息列表。channels: 频道名列表。"""
    items = []
    for ch in channels:
        if budget and budget.expired():
            log("Telegram 采集提前停止（总预算耗尽），剩余频道跳过")
            break
        url = "https://t.me/s/%s" % ch
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
            })
            with urllib.request.urlopen(req, timeout=budget.timeout(20) if budget else 20) as r:
                html = r.read(500000).decode("utf-8", errors="ignore")
        except Exception as e:
            log("Telegram %s 抓取失败: %s" % (ch, e))
            continue
        # 按消息块分割：每个消息从 tgme_widget_message_wrap 开始
        msg_raws = re.split(r'<div class="tgme_widget_message_wrap', html)
        time_blocks = re.findall(r'<time datetime="([^"]+)"', html)
        t_idx = 0
        ch_count = 0
        for block in msg_raws[1:]:  # 跳过第一个（消息前的页面内容）
            if ch_count >= max_per_channel:
                break
            # 提取消息文本区
            text_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if not text_m:
                continue
            text_html = text_m.group(1)
            # 提取所有 <a> 的 href 和去标签文本
            links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text_html, re.S)
            if not links:
                continue
            # 第一个链接通常是主文章链接
            link_url = links[0][0].strip()
            title_raw = re.sub(r"<[^>]+>", "", links[0][1]).strip()
            title = re.sub(r"\s+", " ", title_raw).strip()
            if not title or not link_url or link_url.startswith("?"):
                # 有些频道第一个链接是标签，取第二个
                if len(links) > 1:
                    link_url = links[1][0].strip()
                    title_raw = re.sub(r"<[^>]+>", "", links[1][1]).strip()
                    title = re.sub(r"\s+", " ", title_raw).strip()
                if not title or not link_url:
                    continue
            # 跳过 t.me 内部链接（频道自身的消息链接）
            if "t.me/" in link_url and "/s/" not in link_url:
                for href, txt in links[1:]:
                    href = href.strip()
                    txt_clean = re.sub(r"<[^>]+>", "", txt).strip()
                    if href and not href.startswith("?") and "t.me/" not in href:
                        link_url = href
                        title = re.sub(r"\s+", " ", txt_clean).strip()
                        break
                else:
                    continue
            # 内容：去标签，拼接整个消息文本
            content = re.sub(r"<[^>]+>", " ", text_html)
            content = re.sub(r"\s+", " ", content).strip()
            # 时间
            pub_date = ""
            if t_idx < len(time_blocks):
                try:
                    dt = datetime.fromisoformat(time_blocks[t_idx].replace("Z", "+00:00"))
                    pub_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    pub_date = time_blocks[t_idx][:10]
            t_idx += 1
            items.append({
                "title": title[:200],
                "url": link_url,
                "content": "Telegram @%s: %s" % (ch, content[:2000]),
                "publish_date": pub_date,
                "date_verified": bool(pub_date),
                "direction": None,
                "is_preferred": False,
                "is_explore": False,
                "source": "telegram",
                "telegram_channel": ch,
            })
            ch_count += 1
        log("Telegram @%s: %d 条" % (ch, ch_count))
    log("Telegram 合计 %d 条" % len(items))
    return items


NODE_BIN = "/Users/qqiang/.hermes/node/bin/node"
FETCH_ACS_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "fetch_acs.mjs")

def fetch_agent_case_share(limit=5, budget=None):
    """Agent Case Share AI 新闻日报（MCP 列表 + 页面抓全文，Node 通道）。
    失败静默返回 []，不阻断其余源（局部故障隔离）。"""
    items = []
    if budget and budget.expired():
        log("ACS 采集跳过（总预算耗尽）")
        return items
    try:
        p = subprocess.run([NODE_BIN, FETCH_ACS_SCRIPT, "--limit", str(limit)],
                           capture_output=True, text=True,
                           timeout=budget.timeout(ACS_TIMEOUT) if budget else ACS_TIMEOUT)
        if p.returncode != 0:
            log("ACS 采集失败(%d): %s" % (p.returncode, (p.stderr or "")[:200]))
            return items
        raw = json.loads(p.stdout or "[]")
    except Exception as e:
        log("ACS 采集异常: %s" % e)
        return items
    for it in raw:
        items.append({
            "title": (it.get("title") or "")[:200],
            "url": it.get("url") or "",
            "content": it.get("content") or "",
            "publish_date": it.get("item_date") or "",
            "date_verified": bool(it.get("item_date")),
            "direction": "AI",
            "is_preferred": False,
            "is_explore": False,
            "source": "agent_case_share",
        })
    log("Agent Case Share AI日报: %d 条" % len(items))
    return items


def chunked(seq, n=5):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]

def wc(text):
    if not text:
        return 0
    total = len(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return total if cjk >= max(20, total * 0.3) else len(text.split())
