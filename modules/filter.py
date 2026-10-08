import json
import os
import re
from datetime import datetime
from urllib.parse import urlsplit

from .window import BRIEF_DIR, OUTPUT_DIR, log

DEDUP_FILE = os.path.join(BRIEF_DIR, "data", "_dedup_urls.json")
MIN_WORDS = 500
MIN_WORDS_ABSENT = 300
# 正文存储/抓取上限：英文 500 词 ≈ 3000+ 字符，3000 会把合格英文正文截到 500 词以下
CONTENT_LIMIT = 6000
EXCLUDE_DOMAINS = {"zh.wikipedia.org", "en.wikipedia.org", "www.china-emu.cn", "zhuanlan.zhihu.com", "www.britannica.com", "www.unesco.org", "setr.stanford.edu", "technav.ieee.org", "www.chinairn.com", "baike.baidu.com", "www.163.com", "www.dictionary.com", "en.wiktionary.org", "www.collinsdictionary.com", "mathworld.wolfram.com", "www.instagram.com", "www.sciencedirect.com", "knowhow.distrelec.com", "www.maglevboard.net", "www.mlit.go.jp", "www.linear-museum.pref.yamanashi.jp", "imech.cas.cn", "jobs.theguardian.com"}
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "ref_url", "cmpid", "spm", "from", "share_token", "share_source", "source")
TEMPLATE_MARKERS = ["值得注意的是", "首先", "其次", "最后", "综上所述", "不得不说", "在当今"]
AUTHOR_RE = re.compile(r"(作者[：:]\s*\S{1,12}|文\s*[／/]\s*\S{1,12}|撰[文稿]\s*[：:]?\s*\S{1,12}|\bBy\s+[A-Z][\w.·-]{1,30}|\bPosted\s+by\s+\S+|文\s*/\s*\S{2,10})", re.I)
TITLE_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(20\d{2})")
FULL_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{2})/")
URL_DATE_COMPACT_RE = re.compile(r"/(20\d{2})(\d{2})(\d{2})")
URL_DATE_DASH_RE = re.compile(r"/(20\d{2})-(\d{2})-(\d{2})")
URL_DATE_SPLIT_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?=/|\.|$)")

# 英文月份（byline / metadata 解析用）
MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
EN_DATE_YMD_RE = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I)

# 明确署名发布时间标记：只有标记附近的日期才算发布时间证据；
# 普通正文第一处日期（常为事件时间）不得冒充发布时间。
BYLINE_MARK_RE = re.compile(
    r"(发布时间|发布于|发表于|出版日期|日期|Published(?:\s+on)?|Updated(?:\s+on)?|Posted(?:\s+on)?)\s*[：:]?\s*", re.I)

# metadata 中视为发布时间的键（优先级从左到右在调用处控制）
META_DATE_KEYS = ("datePublished", "datepublished", "article:published_time",
                  "parsely-pub-date", "sailthru.date", "pubdate", "publishdate",
                  "publish_date", "date", "dcterms.date", "dc.date", "dc.date.issued")


def _valid_ymd(y, mo, d):
    """合法日历日期校验：2000–2100 且真实存在（2026-02-30 之类拒收）。"""
    try:
        datetime(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return False
    return 2000 <= int(y) <= 2100


def _iso_day(y, mo, d):
    return "%04d-%02d-%02d" % (int(y), int(mo), int(d))


def _parse_meta_date(raw):
    """从 metadata 值（ISO / RFC / 英文月名）解析日精度日期，失败返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", s)
    if m and _valid_ymd(*m.groups()):
        return _iso_day(*m.groups())
    m = EN_DATE_YMD_RE.search(s)
    if m:
        mo = MONTHS_EN.get(m.group(1).lower()[:3])
        if mo and _valid_ymd(m.group(3), mo, m.group(2)):
            return _iso_day(m.group(3), mo, m.group(2))
    return None


def _evidence(date, source, evidence, precision="day"):
    return {"publish_date": date, "date_source": source,
            "evidence": (evidence or "")[:200], "precision": precision}


NO_DATE_EVIDENCE = {"publish_date": None, "date_source": None, "evidence": "", "precision": None}


def extract_date_evidence(item):
    """从文章真实 metadata / 明确署名发布时间 / URL / 标题提取发布日期证据。

    证据优先级：JSON-LD → HTML meta → 署名发布时间（byline 标记附近）→ URL 日精度
    → 标题日精度 → URL 月精度。普通正文第一处日期不算发布时间证据（常为事件时间）。
    所有日期先过合法日历校验。返回 dict；无证据时 publish_date 为 None。
    """
    url, title = item.get("url", "") or "", item.get("title", "") or ""
    head = ((item.get("content", "") or "") + " " + title)[:600]
    metadata = item.get("metadata") or {}

    # 1) JSON-LD（结构化数据，最强证据）
    jld = metadata.get("json_ld") or {}
    for key in ("datePublished", "dateCreated"):
        day = _parse_meta_date(jld.get(key))
        if day:
            return _evidence(day, "json_ld", "JSON-LD %s=%s" % (key, jld.get(key)))
    # 2) HTML meta（datePublished / article:published_time 等）
    meta = metadata.get("meta") or {}
    for key in META_DATE_KEYS:
        day = _parse_meta_date(meta.get(key))
        if day:
            return _evidence(day, "html_meta", "meta[%s]=%s" % (key, meta.get(key)))
    # 2.5) <time datetime> 结构化标签（文章页通常为发布时间）
    for t in metadata.get("time") or []:
        day = _parse_meta_date(t)
        if day:
            return _evidence(day, "html_time", "time[datetime]=%s" % t)
    # 3) 署名发布时间：标记词附近的日期才算
    for mark in BYLINE_MARK_RE.finditer(head):
        seg = head[mark.end():mark.end() + 60]
        m = FULL_DATE_RE.search(seg)
        if m and _valid_ymd(*m.groups()):
            return _evidence(_iso_day(*m.groups()), "byline", mark.group(0) + seg.strip()[:40])
        m = EN_DATE_YMD_RE.search(seg)
        if m:
            mo = MONTHS_EN.get(m.group(1).lower()[:3])
            if mo and _valid_ymd(m.group(3), mo, m.group(2)):
                return _evidence(_iso_day(m.group(3), mo, m.group(2)), "byline",
                                 mark.group(0) + seg.strip()[:40])
    # 4) URL 日精度
    for rx in (URL_DATE_SPLIT_RE, URL_DATE_DASH_RE, URL_DATE_COMPACT_RE):
        m = rx.search(url)
        if m and _valid_ymd(*m.groups()):
            return _evidence(_iso_day(*m.groups()), "url", url[:120])
    # 5) 标题日精度
    m = TITLE_DATE_RE.search(title)
    if m:
        mo, d, y = m.groups()
        if _valid_ymd(y, mo, d):
            return _evidence(_iso_day(y, mo, d), "title", title[:120])
    # 6) URL 月精度（兜底线索，验证层按非日精度拒收）
    m = URL_DATE_RE.search(url)
    if m and _valid_ymd(m.group(1), m.group(2), 1):
        return _evidence("%s-%s-01" % (m.group(1), m.group(2)), "url", url[:120], "month")
    return dict(NO_DATE_EVIDENCE)


def extract_publish_date(item):
    """兼容旧接口：返回 (publish_date, verified, month_only)。
    新代码请用 extract_date_evidence 拿 date_source/evidence/precision。"""
    ev = extract_date_evidence(item)
    pub = ev["publish_date"]
    return pub, bool(pub), ev["precision"] == "month"

# URL 非法字符：反斜杠/空白/控制字符出现即判畸形，拒收而非静默修复
URL_BAD_CHARS_RE = re.compile(r"[\\\s\x00-\x1f\x7f]")

def is_valid_url(u):
    """严格合法性检查：scheme 必须 http/https、netloc 非空、无畸形字符。"""
    if not u or URL_BAD_CHARS_RE.search(u):
        return False
    if not u.startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(u)
    except Exception:
        return False
    return bool(parts.netloc)

def norm_url(u):
    """规范化 URL；畸形 URL 返回 ""（拒收，不静默修复）。"""
    if not u:
        return ""
    u = u.strip().strip(".,;:!?)]}\"'")
    if not u or not u.startswith("http"):
        return ""
    if u.startswith("//"): u = "https:" + u
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
    if not is_valid_url(u):
        return ""
    u = re.sub(r"^https://www\.", "https://", u).split("#", 1)[0]
    if "?" in u:
        base, _, query = u.partition("?")
        keep = [p for p in query.split("&") if p and not any(p.startswith(t) for t in TRACKING_PARAMS)]
        u = base + ("?" + "&".join(keep) if keep else "")
    return u.rstrip("/")

def load_dedup():
    if os.path.exists(DEDUP_FILE):
        try:
            with open(DEDUP_FILE, encoding="utf-8") as f:
                return set(norm_url(u) for u in json.load(f) if isinstance(u, str) and norm_url(u))
        except Exception as e: log("读去重文件失败: %s" % e)
    return set()

def auto_update_dedup():
    existing, urls = load_dedup(), set(load_dedup())
    if os.path.isdir(OUTPUT_DIR):
        for f in os.listdir(OUTPUT_DIR):
            if not re.match(r"Daily-Brief-\d{4}-\d{2}-\d{2}[^.]*\.md$", f): continue
            try:
                with open(os.path.join(OUTPUT_DIR, f), encoding="utf-8", errors="ignore") as fh: text = fh.read()
                for m in re.finditer(r"https?://[^\s)\]>]+", text):
                    u = norm_url(m.group(0))
                    if u: urls.add(u)
            except Exception as e: log("扫描简报 %s 失败: %s" % (f, e))
    try:
        with open(DEDUP_FILE, "w", encoding="utf-8") as f: json.dump(sorted(urls), f, ensure_ascii=False, indent=1)
        log("去重集合自动更新: %d 条（新增 %d）" % (len(urls), len(urls - existing)))
    except Exception as e: log("写去重文件失败: %s" % e)
    return urls

def ai_watermark_check(content):
    reasons = []
    if len(re.findall(r"\d", content)) < 3: reasons.append("无数字/数据")
    if sum(1 for t in TEMPLATE_MARKERS if t in content) >= 2: reasons.append("模板句高频")
    if not AUTHOR_RE.search(content): reasons.append("无署名")
    return len(reasons) >= 3, reasons
