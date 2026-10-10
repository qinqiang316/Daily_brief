# -*- coding: utf-8 -*-
"""视野拓展（horizon）源：无 RSS 索引或高墙来源的标题/短讯采集。

分两档处理（2026-10-10 强哥拍板）：
  - prefer_fulltext=True：先按正常标准抓全文；正文 ≥ MIN_WORDS 且日期证据精确到日、
    落在本次窗口内，则升级进候选池（与其它源同一闸门）；抓不到全文就降级为标题/短讯条目。
  - prefer_fulltext=False（付费墙/严格反爬）：只取订阅源的标题 + 摘要 + 原文发布日期。

视野拓展条目只允许写进简报文末「## 视野拓展」区，不得进深度总结/TLDR/快读。
日期一律取订阅条目自带的原文发布日期（pubDate / dc:date / published），再回退到
filter 的日期证据提取（URL / 标题 / 文章页 JSON-LD）；仍取不到就丢弃，绝不猜测。
无日期条目最多补抓 MAX_DATE_PROBE 篇文章页。URL 或标题里已有日精度日期的不占这份额。
"""
import html as html_lib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from modules import filter as filter_mod
from modules import retrieve
from modules.window import TZ, log

CST = timezone(timedelta(hours=8))

# 每个源声明：订阅地址、展示名、方向、地区、是否优先抓全文
HORIZON_SOURCES = [
    {
        "key": "mit_tr",
        "label": "MIT科技评论",
        "region": "美国",
        "direction": "科技",
        "feed": "https://www.technologyreview.com/feed/",
        "prefer_fulltext": True,
    },
    {
        "key": "ieee_spectrum",
        "label": "IEEE Spectrum",
        "region": "美国",
        "direction": "科技",
        "feed": "https://spectrum.ieee.org/feeds/feed.rss",
        "prefer_fulltext": True,
    },
    {
        "key": "railway_gazette",
        "label": "Railway Gazette",
        "region": "欧洲",
        "direction": "轨道交通",
        "feed": "https://www.railwaygazette.com/feed",
        "prefer_fulltext": False,
    },
    {
        "key": "nikkei_asia",
        "label": "日经亚洲",
        "region": "日本",
        "direction": "商业",
        "feed": "https://asia.nikkei.com/rss/feed/nar",
        "prefer_fulltext": False,
    },
    {
        "key": "nikkei_xtech",
        "label": "日经xTECH",
        "region": "日本",
        "direction": "科技",
        "feed": "https://xtech.nikkei.com/rss/index.rdf",
        "prefer_fulltext": False,
    },
    # 2026-10-10 实测：/feed、/feed.xml、/rss、/rss.xml、/atom.xml、/index.xml、
    # /feed.json 全部 404，首页也没有 <link rel="alternate">。
    # 首页是 PHP + Vue。公开列表是 POST（无登录）news/get-news-data，
    # 参数 page、limit；GET 同一地址返回的是空 HTML，不是订阅。
    # 列表字段 release_time 只有「今天」「10月10日」，没有年份，而且和文章页对不上
    # （id=3752 列表写「10月10日」，页面脚本却是 var release_time='2026/10/01'）。
    # 所以列表时间一律不当原文发布日期。
    # 文章页 `var release_time='YYYY/MM/DD'` 才是原文日期（retrieve 收进 metadata）。
    # 正文由服务端输出，能超过 500 字，因此仍走全文闸门；不够或日期不在窗口就降级/丢弃。
    {
        "key": "latepost",
        "label": "晚点LatePost",
        "region": "中国",
        "direction": "商业",
        "feed": "https://www.latepost.com/news/get-news-data",
        "kind": "latepost",
        "prefer_fulltext": True,
    },
]

FEED_TIMEOUT = 10       # 单个订阅源墙钟上限
FEED_MAX_BYTES = 120000
FEED_RETRIES = 2        # TLS 抖动（EOF / 握手超时）重试次数；轨交源偶发掉线，留一档余量
FEED_BACKOFF_S = 1.5    # 重试间隔，避开瞬时限流
MAX_PER_SOURCE = 3      # 每源最多入视野拓展条数
MAX_HORIZON = 14        # 视野拓展总量上限
MAX_DATE_PROBE = 2      # 无日期条目最多补抓几篇文章页取发布日期

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_ITEM_RE = re.compile(r"<item\b.*?</item>|<entry\b.*?</entry>|<item\b[^>]*/>", re.S | re.I)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def _ssl_ctx():
    try:
        return ssl.create_default_context()
    except Exception:
        return None


def fetch_feed(url, timeout=FEED_TIMEOUT):
    """抓订阅源原文（RSS/RDF/Atom），失败返回 None。只读前 FEED_MAX_BYTES 字节。
    订阅站 TLS 抖动常见（EOF / 握手超时），失败退避重试 FEED_RETRIES 次。"""
    last = None
    for attempt in range(FEED_RETRIES + 1):
        if attempt:
            time.sleep(FEED_BACKOFF_S)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                return r.read(FEED_MAX_BYTES).decode("utf-8", errors="ignore")
        except Exception as e:
            last = e
    log("视野拓展订阅失败(%s): %s" % (url, last))
    return None


def _clean(raw):
    if not raw:
        return ""
    txt = _CDATA_RE.sub(r"\1", raw)
    txt = html_lib.unescape(txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _tags(block):
    """返回 {小写标签名: 首个文本值}。
    先剥掉 item/entry 包裹标签：正则的最左匹配会让 <item>…</item> 整体胜出、吞掉子标签。
    """
    inner = re.sub(r"</?\s*(?:item|entry)\b[^>]*>", " ", block, flags=re.I)
    out = {}
    for m in re.finditer(r"<([A-Za-z][\w:.-]*)(?:\s[^>]*)?>(.*?)</\1\s*>", inner, re.S):
        out.setdefault(m.group(1).lower(), _clean(m.group(2)))
    for m in re.finditer(r"<\s*([A-Za-z][\w:.-]*)(?:\s[^>]*)?/\s*>", inner):
        out.setdefault(m.group(1).lower(), "")
    return out


def _tag(tags, *names):
    for name in names:
        val = tags.get(name.lower())
        if val:
            return val
    return ""


def _parse_feed_datetime(raw):
    """把订阅里的 RFC2822 / ISO8601 收成上海时区的合法 YYYY-MM-DD。失败返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(CST).strftime("%Y-%m-%d")
        if filter_mod.is_iso_day(day):
            return day
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # 纯日期或无时区：按字面日历日，不再平移
            day = dt.strftime("%Y-%m-%d")
        else:
            day = dt.astimezone(CST).strftime("%Y-%m-%d")
        if filter_mod.is_iso_day(day):
            return day
    except Exception:
        pass
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if m:
        day = filter_mod._iso_day(*m.groups())
        if filter_mod.is_iso_day(day):
            return day
    return None


def _entry_date(tags):
    """取订阅条目自带的原文发布日期，返回 (ISO 日期, 证据) 或 (None, None)。
    只认 pubDate / dc:date / published；updated 仅在前面都没有时作最后兜底。
    非法日历日丢弃，不把残缺日期补成年月。"""
    for tag in ("pubdate", "dc:date", "published", "date", "updated"):
        raw = tags.get(tag)
        day = _parse_feed_datetime(raw)
        if day:
            return day, "%s=%s" % (tag, raw)
    return None, None


def _entry_href(block):
    for m in re.finditer(r'<link[^>]*\bhref="([^"]+)"', block, re.I):
        return m.group(1).strip()
    return ""


def parse_feed(text):
    """解析订阅源为 [{'title','url','publish_date','date_evidence','snippet'}]。"""
    out = []
    for block in _ITEM_RE.findall(text or ""):
        tags = _tags(block)
        title = _tag(tags, "title")
        url = _tag(tags, "link") or _entry_href(block)
        if not url or not url.startswith("http") or not title:
            continue
        pub, ev = _entry_date(tags)
        snippet = _tag(tags, "description", "summary", "content")
        out.append({"title": title, "url": url, "publish_date": pub,
                    "date_evidence": ev, "snippet": snippet[:600]})
    return out


def _live_url(url):
    """入库用规范 URL（去掉 www）。晚点文章页去掉 www 会 404，抓取时加回。"""
    if url.startswith("https://latepost.com/"):
        return "https://www.latepost.com/" + url[len("https://latepost.com/"):]
    return url


def _resolve_date(item, budget=None, allow_probe=True):
    """先订阅自带发布时间，再回退 filter 日期证据（URL/标题）；
    allow_probe 时才抓文章页取 JSON-LD/meta 发布日期。
    返回 (date, source, evidence, precision)；无证据一律 (None, None, None, None)，不猜日期。
    budget is None 表示没有总预算，不是「禁止补抓」——禁止补抓走 allow_probe=False。"""
    pub = item.get("publish_date")
    if pub and filter_mod.is_iso_day(pub):
        ev = item.get("date_evidence") or ""
        if ev:
            return pub, "feed_pubdate", ev, "day"
    ev = filter_mod.extract_date_evidence({"url": item.get("url", ""), "title": item.get("title", "")})
    if ev.get("publish_date") and ev.get("date_source") and ev.get("precision") == "day":
        return ev["publish_date"], ev["date_source"], ev["evidence"], "day"
    if not allow_probe or (budget is not None and budget.expired()):
        return None, None, None, None
    art = retrieve.fetch_article(_live_url(item.get("url", "")),
                                 timeout=budget.timeout(8) if budget else 8)
    if art and art.get("metadata"):
        ev2 = filter_mod.extract_date_evidence({"url": item.get("url", ""),
                                                "title": item.get("title", ""),
                                                "metadata": art["metadata"]})
        if ev2.get("publish_date") and ev2.get("date_source") and ev2.get("precision") == "day":
            return ev2["publish_date"], ev2["date_source"], ev2["evidence"], "day"
    return None, None, None, None


def fetch_latepost_entries(url, timeout=FEED_TIMEOUT):
    """晚点公开列表：POST JSON。失败返回 None；成功返回与 parse_feed 相同的条目结构。
    列表里的 release_time 没有年份，不写入 publish_date。"""
    body = urllib.parse.urlencode({"page": "1", "limit": "8"}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.latepost.com/",
    })
    last = None
    payload = None
    for _attempt in range(FEED_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                payload = json.loads(r.read(FEED_MAX_BYTES).decode("utf-8", "ignore"))
            break
        except Exception as e:
            last = e
            payload = None
    if payload is None:
        log("视野拓展订阅失败(%s): %s" % (url, last))
        return None
    try:
        code = int(payload.get("code"))
    except (AttributeError, TypeError, ValueError):
        return []
    if code != 1 or not isinstance(payload.get("data"), list):
        return []
    out = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        title = _clean(str(row.get("title") or ""))
        path = str(row.get("detail_url") or "").strip()
        if not title or not path:
            continue
        if path.startswith("/"):
            path = "https://www.latepost.com" + path
        if not path.startswith("http"):
            continue
        snippet = _clean(str(row.get("abstract") or row.get("intro") or ""))[:600]
        out.append({"title": title, "url": path, "publish_date": None,
                    "date_evidence": None, "snippet": snippet})
    return out


def collect_source(src, dedup, window_start, window_end, budget=None):
    """采集单个视野拓展源。返回 (candidates, horizon_items, status)。
    candidates 为可进候选池的全文条目；status 非空表示本源缺货原因（供简报如实标注）。"""
    if os.environ.get("DAILYBRIEF_HORIZON_OFF"):
        return [], [], None
    if budget and budget.expired():
        log("视野拓展跳过（总预算耗尽）: %s" % src["label"])
        return [], [], "总预算耗尽未采"
    timeout = budget.timeout(FEED_TIMEOUT) if budget else FEED_TIMEOUT
    if src.get("kind") == "latepost":
        entries = fetch_latepost_entries(src["feed"], timeout=timeout)
        if entries is None:
            return [], [], "订阅源不可用（网络失败或订阅地址失效）"
    else:
        raw = fetch_feed(src["feed"], timeout=timeout)
        if not raw:
            return [], [], "订阅源不可用（网络失败或订阅地址失效）"
        entries = parse_feed(raw)
    if not entries:
        return [], [], "订阅源无可用条目（格式变更或内容为空）"

    ws, we = window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    picked, promoted = [], []
    seen = set()
    probes = 0
    saw_article = False
    for it in entries:
        if len(picked) + len(promoted) >= MAX_PER_SOURCE:
            break
        url = filter_mod.norm_url(it["url"])
        if not url or url in dedup or url in seen or filter_mod.is_aggregate_url(url):
            continue
        domain = url.split("/")[2].lower() if "://" in url else ""
        if domain in filter_mod.EXCLUDE_DOMAINS:
            continue
        saw_article = True
        # 先用订阅/URL/标题里已有的日精度证据；都没有才补抓文章页，且限 MAX_DATE_PROBE 次。
        # budget=None 不能拿来表示「不要补抓」，否则上限会被绕开。
        pub, dsrc, dev, prec = _resolve_date(it, budget=budget, allow_probe=False)
        if not pub and probes < MAX_DATE_PROBE:
            probes += 1
            pub, dsrc, dev, prec = _resolve_date(it, budget=budget, allow_probe=True)
        if not pub or prec != "day" or not (ws <= pub <= we):
            continue  # 无日精度窗口内证据 → 丢弃，不猜日期
        seen.add(url)
        picked.append({
            "title": it["title"][:200], "url": url, "domain": domain,
            "source_key": "horizon:" + src["key"], "source_label": src["label"],
            "region": src["region"], "direction": src["direction"],
            "publish_date": pub, "date_verified": True, "date_source": dsrc,
            "date_evidence": dev, "date_precision": "day",
            "snippet": it.get("snippet") or "",
        })

    if picked and src.get("prefer_fulltext"):
        live_urls = [_live_url(x["url"]) for x in picked]
        arts_live = retrieve.fetch_texts_parallel(live_urls, timeout=12,
                                                  budget=budget, with_metadata=True)
        arts = {x["url"]: arts_live.get(_live_url(x["url"])) for x in picked}
        kept = []
        for x in picked:
            art = arts.get(x["url"]) or {}
            text = art.get("text") or ""
            if retrieve.wc(text) < filter_mod.MIN_WORDS:
                kept.append(x)  # 抓不到全文 → 保留为标题/短讯
                continue
            ev = filter_mod.extract_date_evidence({"url": x["url"], "title": x["title"],
                                                   "content": text, "metadata": art.get("metadata")})
            if not (ev.get("publish_date") and ev.get("precision") == "day"
                    and ws <= ev["publish_date"] <= we):
                kept.append(x)
                continue
            x2 = dict(x)
            x2.update({"content": text, "metadata": art.get("metadata"),
                       "publish_date": ev["publish_date"], "date_source": ev["date_source"],
                       "date_evidence": ev["evidence"], "date_precision": "day"})
            promoted.append(x2)
        picked = kept
        return promoted, picked, None  # 全文不够或文章页日期对不上 → 退回标题，不算缺货
    if not picked:
        if not saw_article:
            return [], [], None  # 全部是去重/聚合页/黑名单，不是源缺货
        return [], [], "窗口内无可核验原文日期的条目"
    return promoted, picked, None


def fetch_horizon(dedup, window_start, window_end, budget=None, sources=None):
    """遍历全部视野拓展源。返回 (promoted_candidates, horizon_items, unavailable)。
    sources 可注入（测试用），缺省走 HORIZON_SOURCES。
    unavailable 记录订阅失败/日期全部未验证的源，供简报如实标注缺货。
    DAILYBRIEF_HORIZON_OFF=1 时整体关闭（供测试隔离网络）。"""
    promoted, horizon, unavailable = [], [], []
    if os.environ.get("DAILYBRIEF_HORIZON_OFF"):
        return promoted, horizon, unavailable
    # 拷贝去重集合：源与源之间不重复，也不改调用方的集合
    # （调用方还要用原集合过滤升级进候选池的全文）
    seen_urls = set(dedup)
    for src in (sources if sources is not None else HORIZON_SOURCES):
        if budget and budget.expired():
            log("视野拓展提前停止（总预算耗尽），剩余源跳过")
            break
        try:
            got, items, status = collect_source(src, seen_urls, window_start, window_end, budget=budget)
        except Exception as e:
            log("视野拓展源异常(%s): %s" % (src["label"], e))
            unavailable.append({"source_label": src["label"], "region": src["region"],
                                "direction": src["direction"], "reason": "采集异常：%s" % e})
            continue
        promoted.extend(got)
        horizon.extend(items)
        for row in got + items:
            if row.get("url"):
                seen_urls.add(row["url"])
        if status:
            unavailable.append({"source_label": src["label"], "region": src["region"],
                                "direction": src["direction"], "reason": status})
    # 全局上限：按日期倒序保留最新
    horizon.sort(key=lambda x: x.get("publish_date") or "", reverse=True)
    return promoted, horizon[:MAX_HORIZON], unavailable
