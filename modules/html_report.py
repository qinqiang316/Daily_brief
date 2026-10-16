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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
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
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            if data.strip():
                self.style_text.append(data)
            return
        self.text_parts.append(data)

    def text(self):
        return _norm("".join(self.text_parts))

    def css(self):
        return " ".join(self.style_text)


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
    rej = article_hrefs & rejected_urls
    if rej:
        errors.append("HTML 出现被拒收 URL（审核拒收内容不得交付）: %s"
                      % " ".join(sorted(rej)[:5]))

    # MD 参考资料 URL/标题齐全
    for nu, (n, title) in sorted(ref_urls.items()):
        if nu not in article_hrefs:
            errors.append("HTML 缺参考资料 [%d] 的原文链接: %s" % (n, nu))
        if _norm(title) and _norm(title)[:20] not in html_text:
            errors.append("HTML 缺参考资料 [%d] 的标题: %s" % (n, title[:40]))

    # 摘要一致性：深度总结正文片段必须在 HTML 可见文本中出现
    for snip in _md_deep_summaries(brief_text):
        if snip not in html_text:
            errors.append("HTML 摘要与 canonical MD 不一致（缺片段: %s…）" % snip[:20])

    # 点赞映射：HTML 点赞链接与参考资料一一对应（无拒收 URL 的点赞、无陈旧引用）
    like_urls = like_urls_from_html(scan)
    if like_urls != set(ref_urls):
        errors.append("HTML 点赞映射与参考资料不一致（点赞 %d 条 ≠ 参考资料 %d 条）"
                      % (len(like_urls), len(ref_urls)))
    if like_urls & rejected_urls:
        errors.append("HTML 含被拒收 URL 的点赞链接")

    # 离线/移动端硬性要求
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
