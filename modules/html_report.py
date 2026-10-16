# -*- coding: utf-8 -*-
"""HTML 交付物：点赞区重建 + 与 canonical Markdown 的一致性校验。

交付结构：Markdown 是内部 canonical（走原 validate_brief 强校验闸门）；
HTML 是发布入口（中文大字号、目录、主题/地区筛选、证据折叠、离线无 CDN、
移动端 390px 无溢出）。HTML 正文由小g基于同批 JSON+MD 撰写，本模块负责：
  1. 重建 HTML 点赞区（脚本权威生成，与 MD 参考资料严格一致，标记位替换）；
  2. 校验 HTML 与 canonical 的条目/URL/摘要/点赞映射一致：小g 未验的 HTML
     不得交付——条目缺漏、URL 不在候选池、摘要与 MD 不符、点赞映射错、
     混入拒收 URL 或外部 CDN 资源，任一项即 FAIL。
"""
import html as html_lib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCRIPTS_DIR = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from modules import filter as filter_mod  # noqa: E402

LIKE_BASE = "http://127.0.0.1:8900/like"
LIKE_START = "<!-- LIKE_SECTION_START -->"
LIKE_END = "<!-- LIKE_SECTION_END -->"
RUN_ID_RE = re.compile(r"run_id:\s*([A-Za-z0-9][A-Za-z0-9_-]{0,99})")
LOCAL_LIKE_HOSTS = ("127.0.0.1:8900", "localhost:8900", "127.0.0.1", "localhost")


class _HTMLScan(HTMLParser):
    """收集 href、外部资源标签与可见文本（script/style 内容跳过）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []
        self.external = []
        self.text_parts = []
        self._skip = 0
        self.has_viewport = False
        self.style_text = []
        self.root = {"tag": "root", "attrs": {}, "children": [], "hidden": False}
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = a.get("style") or ""
        hidden = (self.stack[-1]["hidden"] or "hidden" in a or a.get("aria-hidden") == "true"
                  or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[; ]|$)", style, re.I)))
        node = {"tag": tag, "attrs": a, "children": [], "hidden": hidden}
        self.stack[-1]["children"].append(node)
        self.nodes.append(node)
        if tag not in ("meta", "link", "img", "source", "br", "hr", "input", "embed", "wbr"):
            self.stack.append(node)
        for key in ("src", "srcset", "poster", "data", "background"):
            if a.get(key) and not str(a[key]).startswith(("data:", "#")):
                self.external.append("%s[%s]=%s" % (tag, key, a[key]))
        if tag == "link" and a.get("href") and not a["href"].startswith(("data:", "#")):
            self.external.append("link resource")
        if tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
            self.external.append("meta refresh")
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag in ("script", "img", "iframe", "source", "video", "audio") \
                and str(a.get("src", "")).startswith(("http://", "https://")):
            self.external.append("%s[src]=%s" % (tag, a["src"][:120]))
        if tag == "link" and str(a.get("href", "")).startswith(("http://", "https://")):
            self.external.append("link[href]=%s" % a["href"][:120])
        if tag == "meta" and a.get("name") == "viewport":
            self.has_viewport = True

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)
        if self._skip:
            if data.strip():
                self.style_text.append(data)
            return
        if not self.stack[-1]["hidden"]:
            self.text_parts.append(data)

    def text(self):
        return _norm("".join(self.text_parts))

    def css(self):
        return " ".join(self.style_text)


def node_text(node, omit=()):
    if isinstance(node, str):
        return node
    if node["hidden"] or node["tag"] in ("script", "style", "template") + tuple(omit):
        return ""
    return "".join(node_text(c, omit) for c in node["children"])


def descendants(node):
    return [c for c in node["children"] if isinstance(c, dict)] + [d for c in node["children"] if isinstance(c, dict) for d in descendants(c)]


def canonical_articles(text):
    """Full summaries bound to exact citation, not a substring somewhere in the page."""
    if "## 今日热门文章" not in text:
        return []
    section = text.split("## 今日热门文章", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    out = []
    for block in re.split(r"\n#### ", "\n" + section)[1:]:
        title, _, body = block.partition("\n")
        body = re.split(r"\n### ", body)[0]
        cites = re.findall(r"\[\[(\d+)\]\]\((https?://[^)]+)\)", body)
        summary = re.sub(r"\[\[\d+\]\]\(https?://[^)]+\)", "", body)
        # canonical inline emphasis/link syntax -> semantic text
        summary = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", summary)
        summary = re.sub(r"[*`]", "", summary)
        out.append({"title": title.strip(), "summary": _norm(summary), "summary_text": summary.strip(), "cites": cites})
    return out


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def scan_html(path):
    scan = _HTMLScan()
    with open(path, encoding="utf-8", errors="ignore") as f:
        scan.feed(f.read())
    return scan


def _norm_href(u):
    """HTML 里的文章链接规范化后与候选 URL 比对；锚点/本地点赞链接另行处理。"""
    u = (u or "").strip()
    if not u or u.startswith("#"):
        return ""
    return filter_mod.norm_url(u)


def like_urls_from_html(scan):
    """从 HTML 点赞链接提取 url 参数集合（规范化）。"""
    out = set()
    for h in scan.hrefs:
        if not h.startswith(LIKE_BASE):
            continue
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(h).query)
        for raw in qs.get("url") or []:
            nu = filter_mod.norm_url(raw)
            if nu:
                out.add(nu)
    return out


def build_like_html(refs):
    """按参考资料生成 HTML 点赞区（脚本权威内容，结构固定）。"""
    rows = [LIKE_START,
            '<section id="likes"><h2>👍 点赞</h2>',
            '<p>点链接即可为这篇文章点赞（仅本机 127.0.0.1 浏览器有效）；'
            '<strong>手机端无法点击点赞链接</strong>，请在 Hermes 会话发文字指令'
            '「点赞 N」（N 为参考资料序号）。</p>',
            "<ul>"]
    for n in sorted(refs):
        title, url = refs[n]
        q = urllib.parse.urlencode({"url": url, "title": title[:80]})
        rows.append('<li>[%d] %s — <a href="%s?%s">👍 点赞</a></li>'
                    % (n, html_lib.escape(title), LIKE_BASE, q))
    rows += ["</ul></section>", LIKE_END]
    return "\n".join(rows)


def rebuild_like_section(html_path, refs):
    """幂等重建 HTML 点赞区（标记位整体替换）。返回 (changed, msg)。"""
    with open(html_path, encoding="utf-8") as f:
        text = f.read()
    if LIKE_START not in text or LIKE_END not in text:
        return False, "HTML 缺少点赞区标记位 %s / %s" % (LIKE_START, LIKE_END)
    head = text.split(LIKE_START, 1)[0]
    tail = text.split(LIKE_END, 1)[1]
    new_text = head + build_like_html(refs) + tail
    if new_text == text:
        return False, "HTML 点赞区已是最新（幂等，无需重建）"
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(html_path)),
                               prefix=".tmp_html_", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, html_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True, "已重建 HTML 点赞区（%d 条点赞链接）" % len(refs)


def _md_deep_summaries(brief_text):
    """提取 MD 深度总结区每条的摘要片段（去引用标记），用于 HTML 正文一致性抽查。"""
    if "## 今日热门文章" not in brief_text:
        return []
    sec = brief_text.split("## 今日热门文章", 1)[1]
    nxt = re.search(r"\n## ", sec)
    if nxt:
        sec = sec[:nxt.start()]
    snippets = []
    for block in re.split(r"\n#### ", "\n" + sec)[1:]:
        lines = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
        if not lines:
            continue
        body = " ".join(lines)
        body = re.sub(r"\[\[\d+\]\]\(https?://[^)]+\)", "", body)
        body = re.sub(r"https?://\S+", "", body)
        body = _norm(body)
        if len(body) >= 20:
            snippets.append(body[:40])
    return snippets


def verify_html(html_path, brief_path, cand_path):
    """校验 HTML 与 canonical MD/候选 JSON 一致。返回 (ok, errors)。

    校验项：run_id 一致；MD 参考资料每条 URL/标题都在 HTML；HTML 文章链接 ⊆
    候选池（含 leftovers/horizon）；拒收 URL 不得出现；摘要片段与 MD 一致；
    点赞链接与参考资料一一对应；离线无外部资源；移动端 viewport + 长 URL 断行；
    手机文字点赞提示。
    """
    import add_like  # 延迟导入避免循环

    errors = []
    with open(brief_path, encoding="utf-8", errors="ignore") as f:
        brief_text = f.read()
    with open(cand_path, encoding="utf-8") as f:
        payload = json.load(f)
    with open(html_path, encoding="utf-8", errors="ignore") as f:
        html_raw = f.read()

    from modules import controlled_html
    if controlled_html.MARKER in html_raw:
        try:
            if html_raw != controlled_html.render(brief_path, cand_path):
                errors.append("HTML 受控模板/数据被修改，无法证明可见性")
        except (ValueError, KeyError, AttributeError) as exc:
            errors.append("HTML 受控模板生成失败: " + str(exc))
    else:
        # Strict compatibility for pre-template fixtures. No arbitrary CSS/classes/ancestors/script.
        allowed = {"body{font-size:19px;line-height:1.8}a{overflow-wrap:anywhere;word-break:break-word}",
                   "body{font-size:19px;line-height:1.8;max-width:880px;margin:auto;padding:16px}*{box-sizing:border-box;overflow-wrap:anywhere}a{word-break:break-word}"}
        styles = re.findall(r"<style[^>]*>(.*?)</style>", html_raw, re.S | re.I)
        if len(styles) != 1 or re.sub(r"\s+", "", styles[0]) not in allowed:
            errors.append("HTML 隐藏CSS/任意CSS无法证明可见性；必须用受控模板")
        preliminary = _HTMLScan(); preliminary.feed(html_raw)
        if any(n["attrs"].get("style") or any(k.startswith("on") for k in n["attrs"])
               or n["tag"] in ("script", "svg", "math", "iframe", "object", "template", "form")
               for n in preliminary.nodes):
            errors.append("HTML 不允许生成CSS/脚本/隐藏祖先；必须用受控模板")
    scan = scan_html(html_path)
    html_text = scan.text()

    # run_id 一致（HTML 里以注释/文本标注，查原文）
    m_md = RUN_ID_RE.search(brief_text)
    m_html = RUN_ID_RE.search(html_raw)
    if m_md and (not m_html or m_html.group(1) != m_md.group(1)):
        errors.append("HTML run_id 与 canonical MD 不一致")

    refs = add_like.parse_brief_refs(brief_path)
    if not refs:
        errors.append("canonical MD 未解析到参考资料")
    ref_urls = {}
    for n, (title, url) in refs.items():
        nu = filter_mod.norm_url(url)
        if nu:
            ref_urls[nu] = (n, title)

    cand_urls = set()
    rejected_urls = set()
    for key in ("candidates", "source_leftovers", "horizon"):
        for c in payload.get(key) or []:
            u = filter_mod.norm_url(c.get("url", ""))
            if u:
                cand_urls.add(u)
    for r in payload.get("rejected_candidates") or []:
        u = filter_mod.norm_url(r.get("url", ""))
        if u:
            rejected_urls.add(u)

    # HTML 文章链接 ⊆ 候选池；拒收 URL 禁止出现
    article_hrefs = set()
    for h in scan.hrefs:
        if h.startswith(LIKE_BASE):
            continue
        nu = _norm_href(h)
        if not nu:
            continue
        host = nu.split("/")[2].lower() if "://" in nu else ""
        if host in LOCAL_LIKE_HOSTS:
            continue
        article_hrefs.add(nu)
    outside = article_hrefs - cand_urls
    if outside:
        errors.append("HTML %d 条链接不在候选池（未经统一准入/校验）: %s"
                      % (len(outside), " ".join(sorted(outside)[:5])))
    decoded_raw = html_lib.unescape(urllib.parse.unquote(html_raw))
    raw_urls = {filter_mod.norm_url(u.rstrip(".,;，。")) for u in re.findall(r'https?://[^\s<>"\')]+', decoded_raw)}
    rej = (article_hrefs | raw_urls) & rejected_urls
    if rej:
        errors.append("HTML 出现被拒收 URL（审核拒收内容不得交付）: %s"
                      % " ".join(sorted(rej)[:5]))

    # MD 参考资料 URL/标题齐全
    for nu, (n, title) in sorted(ref_urls.items()):
        if nu not in article_hrefs:
            errors.append("HTML 缺参考资料 [%d] 的原文链接: %s" % (n, nu))
        if _norm(title) and _norm(title) not in html_text:
            errors.append("HTML 缺参考资料 [%d] 的标题: %s" % (n, title[:40]))

    # Strict visible semantic schema: no title/summary can be borrowed from another card.
    articles = [n for n in scan.nodes if n["tag"] == "article"]
    canonical = canonical_articles(brief_text)
    if len(articles) != len(canonical):
        errors.append("HTML 摘要与 canonical 条目数不一致")
    used = set()
    for expected in canonical:
        cites = expected["cites"]
        if len(cites) != 1:
            errors.append("canonical 每篇须有唯一编号引用")
            continue
        number, url = cites[0]
        matching = [n for n in articles if n["attrs"].get("data-ref") == number]
        if len(matching) != 1 or number in used:
            errors.append("HTML 摘要与 canonical 编号绑定不一致: " + number)
            continue
        used.add(number)
        article = matching[0]
        fields = descendants(article)
        titles = [n for n in fields if n["attrs"].get("data-role") == "title"]
        summaries = [n for n in fields if n["attrs"].get("data-role") == "summary"]
        citations = [n for n in fields if n["attrs"].get("data-role") == "citation"]
        valid = (not article["hidden"] and article["attrs"].get("data-url") == url
                 and len(titles) == len(summaries) == len(citations) == 1)
        if valid:
            title, summary, citation = titles[0], summaries[0], citations[0]
            valid = (_norm(node_text(title)) == _norm(expected["title"])
                     and _norm(node_text(summary)) == expected["summary"]
                     and citation["tag"] == "a" and citation["attrs"].get("href") == url
                     and _norm(node_text(citation)) == "[%s]原文链接" % number
                     and _norm(node_text(article, ("details",))) ==
                     _norm(expected["title"]) + expected["summary"] + "[%s]原文链接" % number)
        if not valid:
            errors.append("HTML 摘要与 canonical 完整可见语义/标题/URL/编号不一致: " + number)
    # Authoritative like block binds number/title/URL, and detects duplicates, 999, and hidden rows.
    expected_likes = _HTMLScan()
    expected_likes.feed(build_like_html(refs))
    likes = [n for n in scan.nodes if n["attrs"].get("id") == "likes"]
    if len(likes) != 1 or _norm(node_text(likes[0])) != expected_likes.text():
        errors.append("HTML 点赞映射编号/标题/可见文本不一致")
    actual_links = [n["attrs"].get("href") for n in scan.nodes if n["tag"] == "a" and n["attrs"].get("href", "").startswith(LIKE_BASE)]
    if actual_links != expected_likes.hrefs:
        errors.append("HTML 点赞映射完整编号URL序列不一致")

    # 点赞映射：HTML 点赞链接与参考资料一一对应（无拒收 URL 的点赞、无陈旧引用）
    like_urls = like_urls_from_html(scan)
    if like_urls != set(ref_urls):
        errors.append("HTML 点赞映射与参考资料不一致（点赞 %d 条 ≠ 参考资料 %d 条）"
                      % (len(like_urls), len(ref_urls)))
    if like_urls & rejected_urls:
        errors.append("HTML 含被拒收 URL 的点赞链接")

    # 离线/移动端硬性要求
    # Relative resources are also dependencies; CSS imports/url and executable network constructs fail closed.
    css_raw = html_lib.unescape(html_raw)
    if re.search(r"@import|url\s*\(\s*(?![\"']?data:)", css_raw, re.I):
        scan.external.append("CSS @import/url resource")
    if re.search(r"(?:fetch|XMLHttpRequest|WebSocket|importScripts)\s*\(|import\s*\(", scan.css()):
        scan.external.append("script network resource")
    for n in scan.nodes:
        if n["attrs"].get("style"):
            css_raw += n["attrs"]["style"]
    # Static validation cannot evaluate arbitrary hiding CSS. Refuse it, rather than count hidden text.
    if controlled_html.MARKER not in html_raw and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:[;} ]|$)|font-size\s*:\s*0|clip(?:-path)?\s*:", css_raw, re.I):
        errors.append("HTML 隐藏CSS无法证明正文可见，拒绝")
    if scan.external:
        errors.append("HTML 引用外部资源（离线交付禁止 CDN/外链资源）: %s"
                      % " ".join(scan.external[:3]))
    if not scan.has_viewport:
        errors.append("HTML 缺 viewport meta（移动端 390px 适配必要条件）")
    css = scan.css()
    if "overflow-wrap" not in css and "word-break" not in css:
        errors.append("HTML 样式缺 overflow-wrap/word-break（长 URL/code 必须可断行）")
    if "手机" not in html_text or "点赞" not in html_text:
        errors.append("HTML 缺「手机端文字点赞」提示")

    return (not errors), errors


def html_for_brief(brief_path):
    """canonical MD 对应的 HTML 路径（同目录同名 .html）。"""
    base, _ = os.path.splitext(str(brief_path))
    return base + ".html"


def verify_browser_receipt(directory, brief, html):
    """Bind the real browser result to exactly the canonical+HTML being published."""
    import hashlib
    from pathlib import Path
    receipt = None
    html_hash = hashlib.sha256(Path(html).read_bytes()).hexdigest()
    md_hash = hashlib.sha256(Path(brief).read_bytes()).hexdigest()
    # Corrections get a new immutable evidence directory. Never overwrite a
    # previous browser receipt merely to make its old hash match new content.
    for path in sorted(Path(directory).rglob('receipt.json')):
        try:
            candidate = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if (candidate.get('html_sha256') == html_hash
                and candidate.get('md_sha256') == md_hash):
            receipt = candidate
            break
    if receipt is None:
        raise ValueError('缺真实浏览器验收receipt或没有匹配当前HTML/MD的凭据')
    expected = canonical_articles(Path(brief).read_text())
    bodies = receipt.get('bodies') or []
    if (receipt.get('ok') is not True or receipt.get('mode') != 'REAL_BROWSER_OFFLINE'
            or receipt.get('html_sha256') != hashlib.sha256(Path(html).read_bytes()).hexdigest()
            or receipt.get('md_sha256') != hashlib.sha256(Path(brief).read_bytes()).hexdigest()
            or receipt.get('mobile') != {'innerWidth': 390, 'scrollWidth': 390, 'bodyWidth': 390}
            or receipt.get('networkRequests') != [] or receipt.get('pageErrors') != []
            or receipt.get('directory') is not True or len(bodies) != len(expected)):
        raise ValueError('浏览器验收未通过或与待发布HTML/MD hash不符')
    for actual, canonical in zip(bodies, expected):
        if (len(canonical['cites']) != 1 or actual.get('number') != canonical['cites'][0][0]
                or actual.get('url') != canonical['cites'][0][1]
                or actual.get('summaryLength') != len(canonical['summary'])
                or actual.get('fontSize', 0) < 18 or actual.get('initialVisible') is not True
                or actual.get('evidenceExpanded') is not True):
            raise ValueError('浏览器逐篇可见正文/编号URL证据不符')
    return True
