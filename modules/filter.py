import json
import os
import re
from datetime import datetime

from .window import BRIEF_DIR, log

DEDUP_FILE = os.path.join(BRIEF_DIR, "data", "_dedup_urls.json")
MIN_WORDS = 500
MIN_WORDS_ABSENT = 300
CONTENT_LIMIT = 3000
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

def norm_url(u):
    if not u:
        return ""
    u = u.strip().strip(".,;:!?)]}\"'")
    if not u or not u.startswith("http"):
        return ""
    if u.startswith("//"): u = "https:" + u
    if u.startswith("http://"): u = "https://" + u[len("http://"):]
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
    if os.path.isdir(BRIEF_DIR):
        for f in os.listdir(BRIEF_DIR):
            if not re.match(r"Daily-Brief-\d{4}-\d{2}-\d{2}[^.]*\.md$", f): continue
            try:
                with open(os.path.join(BRIEF_DIR, f), encoding="utf-8", errors="ignore") as fh: text = fh.read()
                for m in re.finditer(r"https?://[^\s)\]>]+", text):
                    u = norm_url(m.group(0))
                    if u: urls.add(u)
            except Exception as e: log("扫描简报 %s 失败: %s" % (f, e))
    try:
        with open(DEDUP_FILE, "w", encoding="utf-8") as f: json.dump(sorted(urls), f, ensure_ascii=False, indent=1)
        log("去重集合自动更新: %d 条（新增 %d）" % (len(urls), len(urls - existing)))
    except Exception as e: log("写去重文件失败: %s" % e)
    return urls

def extract_publish_date(item):
    url, head = item["url"], (item.get("content", "") + " " + item.get("title", ""))[:300]
    for rx in (URL_DATE_SPLIT_RE, URL_DATE_DASH_RE, URL_DATE_COMPACT_RE):
        m = rx.search(url)
        if m:
            try:
                y, mo, d = map(int, m.groups())
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31: return "%04d-%02d-%02d" % (y, mo, d), True, False
            except Exception: pass
    m = TITLE_DATE_RE.search(item.get("title", ""))
    if m:
        try: mo, d, y = map(int, m.groups()); return "%04d-%02d-%02d" % (y, mo, d), True, False
        except Exception: pass
    m = URL_DATE_RE.search(url)
    if m:
        try: return "%s-%02d-01" % (m.group(1), int(m.group(2))), True, True
        except Exception: pass
    for mm in FULL_DATE_RE.finditer(head):
        try:
            y, mo, d = map(int, mm.groups())
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31: return "%04d-%02d-%02d" % (y, mo, d), True, False
        except Exception: continue
    return None, False, False

def ai_watermark_check(content):
    reasons = []
    if len(re.findall(r"\d", content)) < 3: reasons.append("无数字/数据")
    if sum(1 for t in TEMPLATE_MARKERS if t in content) >= 2: reasons.append("模板句高频")
    if not AUTHOR_RE.search(content): reasons.append("无署名")
    return len(reasons) >= 3, reasons
