import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta

from .window import TZ, log

CLI = "/Users/qqiang/.hermes/hermes-agent/venv/bin/python"
CLI_SCRIPT = "/Users/qqiang/.hermes/skills/anysearch/scripts/anysearch_cli.py"
TIMEOUT = 60
ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"
ANYSEARCH_ENV_CANDIDATES = [
    "/Users/qqiang/.hermes/skills/anysearch/scripts/.env",
    "/Users/qqiang/.hermes/skills/anysearch/.env",
]

def run_cli(args):
    try:
        p = subprocess.run([CLI, CLI_SCRIPT] + args, capture_output=True,
                           text=True, timeout=TIMEOUT)
        if p.returncode == 0:
            return p.stdout
        log("CLI 失败(%d): %s" % (p.returncode, p.stderr[:200]))
    except subprocess.TimeoutExpired:
        log("CLI 超时(%ss): %s" % (TIMEOUT, " ".join(args)[:120]))
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

def anysearch_http_batch(queries):
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
    if not api_key:
        log("anysearch HTTP 兜底：找不到 API key")
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "batch_search", "arguments": {"queries": queries}}}
    try:
        req = urllib.request.Request(ANYSEARCH_ENDPOINT, data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + api_key}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            items = parse_search_markdown(r.read().decode("utf-8", errors="replace"))
        if items:
            log("anysearch HTTP 兜底成功: %d 条" % len(items))
        return items or None
    except Exception as e:
        log("anysearch HTTP 兜底失败: %s" % e)
        return None

def fetch_article_text(url, timeout=15, limit=2500):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(500000).decode("utf-8", errors="ignore")
        raw = re.sub(r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"\s+", " ", re.sub(r"&[a-z]+;|<[^>]+>", " ", raw)).strip()
        return text[:limit] if len(text) >= 200 else None
    except Exception as e:
        log("预抓 %s 失败: %s" % (url[:60], e))
        return None

def fetch_hn(now):
    items = []
    since = int((now - timedelta(hours=72)).timestamp())
    url = ("https://hn.algolia.com/api/v1/search?tags=story"
           "&numericFilters=created_at_i%%3E%d,points%%3E=20&hitsPerPage=40" % since)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
        for hit in data.get("hits", []):
            pts, created = hit.get("points") or 0, hit.get("created_at") or ""
            pub_date = ""
            if created:
                try:
                    pub_date = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d")
                except Exception:
                    pub_date = created[:10]
            items.append({"title": hit.get("title", ""), "url": hit.get("url") or ("https://news.ycombinator.com/item?id=" + str(hit.get("objectID", ""))), "content": "HN 热议 %d 分。%s" % (pts, hit.get("title", "")), "publish_date": pub_date, "date_verified": bool(pub_date), "hn_points": pts})
        items.sort(key=lambda x: x.get("hn_points") or 0, reverse=True)
        log("HN 拉到 %d 条(≥20分,按分排序)" % len(items))
    except Exception as e:
        log("HN API 失败: %s" % e)
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
