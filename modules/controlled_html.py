"""Deterministic offline presentation. Untrusted inference never supplies executable CSS/JS."""
import html
import json
import re
import sys
from pathlib import Path
from modules.region import normalized
from modules.filter import norm_url

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

CSS = '''*{box-sizing:border-box;min-width:0}html,body{margin:0;padding:0;width:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;color:#1e293b;background-color:#f6f8fa;font-size:18px;line-height:1.8;-webkit-text-size-adjust:100%}main{max-width:880px;margin:0 auto;padding:24px 16px 64px 16px}@media (max-width:640px){main{padding:14px 12px 48px 12px}}article,section,nav,.masthead{background:#ffffff;border-radius:12px;padding:24px 28px;margin:20px 0;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,0.04)}@media (max-width:640px){article,section,nav,.masthead{padding:18px 16px;margin:14px 0;border-radius:10px}}a,p,pre,code,li,details,h1,h2,h3,h4,span,div,blockquote{overflow-wrap:anywhere;word-break:break-word}h1,h2,h3,h4{color:#0f172a;margin:0 0 16px 0;line-height:1.4;font-weight:700}h1{font-size:28px;letter-spacing:-0.02em}h2{font-size:22px;padding-bottom:10px;border-bottom:2px solid #f1f5f9;letter-spacing:-0.01em}h3{font-size:20px;font-weight:600;color:#1e293b}h4{font-size:18px;font-weight:600}p{margin:12px 0;color:#334155}blockquote{margin:12px 0;padding:8px 16px;border-left:4px solid #cbd5e1;background:#f8fafc;color:#475569;font-size:16px}.masthead-badge{display:inline-block;font-size:13px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#1d4ed8;background:#eff6ff;padding:3px 10px;border-radius:9999px;margin-bottom:10px}.meta-bar{display:flex;flex-wrap:wrap;gap:8px 16px;color:#64748b;font-size:15px;margin:12px 0 18px 0;padding-bottom:16px;border-bottom:1px dashed #e2e8f0}.filter-bar{display:flex;flex-wrap:wrap;align-items:center;gap:12px 20px;background:#f8fafc;padding:12px 16px;border-radius:8px;border:1px solid #e2e8f0}.filter-bar label{display:inline-flex;align-items:center;gap:8px;font-size:15px;font-weight:500;color:#475569}select{font:inherit;font-size:15px;padding:6px 12px;border:1px solid #cbd5e1;border-radius:6px;background-color:#ffffff;color:#1e293b;cursor:pointer;outline:none}select:focus{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,0.2)}.section-tldr{background:#f8fafc;border-left:4px solid #2563eb}.section-tldr h2{color:#1d4ed8;border-bottom:1px solid #e2e8f0}nav a{display:block;padding:8px 12px;color:#2563eb;text-decoration:none;border-radius:6px;margin:4px 0;font-size:17px;line-height:1.5;transition:background-color 0.15s}nav a:hover{background:#f1f5f9;text-decoration:underline}article{border-left:4px solid #3b82f6;position:relative}article[data-region="cn"]{border-left-color:#e11d48}article[data-region="intl"]{border-left-color:#2563eb}article[data-region="other"]{border-left-color:#64748b}article[hidden]{display:none!important}h3[data-role="title"]{margin-top:0;margin-bottom:12px}p[data-role="summary"]{margin:12px 0 16px 0;color:#334155}a[data-role="citation"]{display:inline-flex;align-items:center;padding:6px 14px;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;border-radius:6px;text-decoration:none;font-size:15px;font-weight:500;margin-top:8px;transition:all 0.15s}a[data-role="citation"]:hover{background:#dbeafe;text-decoration:underline}details{margin-top:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px}summary{cursor:pointer;font-size:15px;font-weight:600;color:#475569;user-select:none}details pre{white-space:pre-wrap;background:#ffffff;border:1px solid #e2e8f0;padding:10px;border-radius:6px;font-size:14px;color:#334155;margin:8px 0}.ref-link{display:inline-block;color:#1d4ed8;background:#eff6ff;border:1px solid #dbeafe;font-weight:600;font-size:0.85em;line-height:1.3;padding:1px 5px;border-radius:4px;margin:0 3px;text-decoration:none;vertical-align:baseline}.ref-link:hover{background:#dbeafe;border-color:#bfdbfe;text-decoration:none}.ext-link{color:#2563eb;text-decoration:underline;font-size:0.9em}a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}ul,ol{padding-left:24px;margin:12px 0}li{margin:10px 0;line-height:1.75;color:#334155}.ref-list li{margin:8px 0}.ref-num{font-weight:600;color:#1d4ed8;margin-right:6px}.status-note,.muted-note{color:#64748b;font-size:16px;margin:12px 0}'''

JS = '''const cards=[...document.querySelectorAll("article")];const topic=document.querySelector("#topic"),region=document.querySelector("#region");function filter(){let count=0;for(const card of cards){const shown=(!topic.value||card.dataset.direction===topic.value)&&(!region.value||card.dataset.region===region.value);card.hidden=!shown;if(shown)count++;}document.querySelector("#count").textContent=count;for(const link of document.querySelectorAll("nav [data-target]")){link.hidden=document.querySelector("#"+link.dataset.target).hidden;}}if(topic&&region){topic.addEventListener("change",filter);region.addEventListener("change",filter);filter();}'''
MARKER = '<!-- dailybrief-controlled-html:v1 -->'


def is_safe_url(u):
    if not u or not isinstance(u, str):
        return False
    u_clean = u.strip()
    lower = u_clean.lower()
    if lower.startswith(('javascript:', 'data:', 'vbscript:', 'file:')):
        return False
    if not (lower.startswith(('https://', 'http://', '#'))):
        return False
    if any(c in u_clean for c in ('<', '>', '"', "'", '`', '\\', ' ', '\n', '\r', '\t')):
        return False
    return True


def render_inline_md(s, url_to_ref, esc):
    tokens = {}
    idx = 0

    def repl_cite(m):
        nonlocal idx
        n, u = m.group(1), m.group(2).strip()
        if not is_safe_url(u):
            return esc(m.group(0))
        key = f"__TOKEN_CITE_{idx}__"
        idx += 1
        tokens[key] = f'<a href="{esc(u)}" class="ref-link" target="_blank" rel="noopener noreferrer">[{esc(n)}]</a>'
        return key
    s = re.sub(r'\[\[(\d+)\]\]\((https?://[^)\s]+)\)', repl_cite, s)

    def repl_link(m):
        nonlocal idx
        title, u = m.group(1), m.group(2).strip()
        if not is_safe_url(u):
            return esc(m.group(0))
        key = f"__TOKEN_LINK_{idx}__"
        idx += 1
        etitle = esc(title)
        etitle = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', etitle)
        tokens[key] = f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{etitle}</a>'
        return key
    s = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', repl_link, s)

    def repl_paren(m):
        nonlocal idx
        u = m.group(1).strip()
        if not is_safe_url(u):
            return esc(m.group(0))
        key = f"__TOKEN_PAREN_{idx}__"
        idx += 1
        nu = norm_url(u)
        label = f"[{url_to_ref[nu]}]" if nu in url_to_ref else "[来源]"
        tokens[key] = f'<a href="{esc(u)}" class="ref-link" target="_blank" rel="noopener noreferrer">{label}</a>'
        return key
    s = re.sub(r'\((https?://[^)\s]+)\)', repl_paren, s)

    def repl_bare(m):
        nonlocal idx
        u = m.group(1).strip()
        if not is_safe_url(u):
            return esc(m.group(0))
        key = f"__TOKEN_BARE_{idx}__"
        idx += 1
        nu = norm_url(u)
        label = f"[{url_to_ref[nu]}]" if nu in url_to_ref else "[链接]"
        tokens[key] = f'<a href="{esc(u)}" class="ref-link" target="_blank" rel="noopener noreferrer">{label}</a>'
        return key
    s = re.sub(r'(https?://[^\s<>\)\]]+)', repl_bare, s)

    s = esc(s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)

    for k, v in tokens.items():
        s = s.replace(esc(k), v).replace(k, v)
    return s


def render_markdown_block(block_text, url_to_ref, esc):
    lines = block_text.strip().splitlines()
    html_lines = []
    list_items = []
    p_buffer = []

    def flush_p():
        nonlocal p_buffer
        if p_buffer:
            html_lines.append('<p>' + '<br>'.join(render_inline_md(l, url_to_ref, esc) for l in p_buffer) + '</p>')
            p_buffer = []

    def flush_list():
        nonlocal list_items
        if list_items:
            html_lines.append('<ul>')
            for it in list_items:
                html_lines.append('<li>' + it + '</li>')
            html_lines.append('</ul>')
            list_items = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_p()
            flush_list()
            continue

        if line.startswith(('- ', '* ')):
            flush_p()
            content = line[2:].strip()
            list_items.append(render_inline_md(content, url_to_ref, esc))
        elif line.startswith('> '):
            flush_p()
            flush_list()
            html_lines.append('<blockquote>' + render_inline_md(line[2:].strip(), url_to_ref, esc) + '</blockquote>')
        elif line.startswith('#### '):
            flush_p()
            flush_list()
            html_lines.append('<h4>' + render_inline_md(line[5:], url_to_ref, esc) + '</h4>')
        elif line.startswith('### '):
            flush_p()
            flush_list()
            html_lines.append('<h3>' + render_inline_md(line[4:], url_to_ref, esc) + '</h3>')
        elif list_items and not line.startswith('#'):
            list_items[-1] += ' ' + render_inline_md(line, url_to_ref, esc)
        else:
            flush_list()
            p_buffer.append(line)

    flush_p()
    flush_list()
    return '\n'.join(html_lines)


def render(brief, candidate):
    from modules.html_report import canonical_articles, build_like_html, RUN_ID_RE
    import add_like
    text = Path(brief).read_text()
    payload = json.loads(Path(candidate).read_text())
    refs = add_like.parse_brief_refs(brief)
    url_to_ref = {norm_url(u): n for n, (t, u) in refs.items()}
    esc = lambda s: html.escape(str(s), quote=True)
    articles = canonical_articles(text)
    pool = {c['url']: c for k in ('candidates', 'source_leftovers', 'horizon') for c in payload.get(k, [])}
    run_id = RUN_ID_RE.search(text).group(1)
    choices = sorted({pool.get(a['cites'][0][1], {}).get('direction') or '其他' for a in articles if a['cites']})

    m_tw = re.search(r'(时间窗口[：:][^\n]+)', text)
    time_window = m_tw.group(1).strip() if m_tw else ''

    sections = re.split(r'\n(?=## )', text)
    tldr_title = None
    tldr_content = None
    other_sections = []

    sec0 = sections[0]
    if '**今日速览' in sec0:
        idx = sec0.index('**今日速览')
        tldr_raw = sec0[idx:]
        tldr_line, _, tldr_body = tldr_raw.partition('\n')
        tldr_title = tldr_line.strip('* ')
        tldr_content = tldr_body.strip()

    for sec in sections[1:]:
        if sec.startswith(('## 今日热门文章', '## 👍 点赞')):
            continue
        if sec.startswith('## 今日速览'):
            heading_line, _, body = sec.partition('\n')
            tldr_title = heading_line[3:].strip()
            tldr_content = body.strip()
            continue
        other_sections.append(sec)

    out = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>DailyBrief ' + esc(payload.get('window', {}).get('end', '')) + '</title>',
        '<style>' + CSS + '</style></head><body><main>',
        MARKER,
        '<!-- run_id: ' + esc(run_id) + ' -->',
        '<div class="masthead">',
        '<div class="masthead-badge">DailyBrief · 每日精选</div>',
        '<h1>DailyBrief ' + esc(payload.get('window', {}).get('end', '')) + '</h1>',
        '<div class="meta-bar">' + (f'<span class="meta-item">{esc(time_window)}</span>' if time_window else '') +
        f'<span class="meta-item">全部 {len(articles)} 篇 · 当前显示 <span id="count">{len(articles)}</span> 篇</span></div>',
        '<div class="filter-bar"><label>主题 <select id="topic"><option value="">全部</option>' + ''.join('<option>' + esc(c) + '</option>' for c in choices) + '</select></label>',
        '<label>地区 <select id="region"><option value="">全部</option><option value="cn">国内</option><option value="intl">国外</option><option value="other">其他来源</option></select></label></div>',
        '</div>'
    ]

    # TLDR first
    if tldr_content:
        out.append('<section id="tldr" class="section-tldr"><h2>' + esc(tldr_title or '今日速览') + '</h2>\n' + render_markdown_block(tldr_content, url_to_ref, esc) + '</section>')

    # Nav
    out.append('<nav aria-label="目录"><h2>目录</h2>')
    for a in articles:
        if not a['cites']:
            raise ValueError('canonical 每篇至少需要一个引用')
        n, u = a['cites'][0]
        out.append('<a href="#article-' + esc(n) + '" data-target="article-' + esc(n) + '">[' + esc(n) + '] ' + esc(a['title']) + '</a>')
    out.append('</nav>')

    # Deep articles
    out.append('<section id="deep"><h2>今日热门文章</h2>')
    for a in articles:
        n, u = a['cites'][0]
        c = pool.get(u, {})
        out += [
            '<article id="article-' + esc(n) + '" data-ref="' + esc(n) + '" data-url="' + esc(u) + '" data-direction="' + esc(c.get('direction') or '其他') + '" data-region="' + esc(normalized(c.get('region'))) + '">',
            '<h3 data-role="title">' + esc(a['title']) + '</h3>',
            '<p data-role="summary">' + esc(a.get('summary_text', a['summary'])) + '</p>',
            '<a data-role="citation" href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">[' + esc(n) + ']原文链接</a>',
            '<details><summary>日期与原文证据</summary><p>' + esc(c.get('publish_date', '')) + ' ' + esc(c.get('date_source', '')) + '</p><pre>' + esc(c.get('date_evidence', '')) + '</pre><p>' + esc(u) + '</p></details></article>'
        ]
    out.append('</section>')

    # Remaining sections
    for sec in other_sections:
        if sec.startswith('## 参考资料'):
            heading, _, body = sec.partition('\n')
            status_lines = [l.strip() for l in body.splitlines() if l.strip() and not re.match(r'^\[\d+\]', l.strip())]
            ref_items = []
            for n, (t, u) in sorted(refs.items()):
                ref_items.append('<li><span class="ref-num">[%d]</span> <a href="%s" target="_blank" rel="noopener noreferrer">%s</a></li>' % (n, esc(u), esc(t)))
            sec_html = ['<section id="references"><h2>参考资料</h2><ul class="ref-list">']
            sec_html.extend(ref_items)
            sec_html.append('</ul>')
            if status_lines:
                sec_html.append('<p class="status-note">' + esc(' '.join(status_lines)) + '</p>')
            sec_html.append('</section>')
            out.append('\n'.join(sec_html))
        elif sec.startswith('## '):
            heading, _, body = sec.partition('\n')
            htitle = heading[3:].strip()
            slug = ('trends' if '趋势' in htitle else 'perspectives' if '观点' in htitle
                    else 'quick' if '快速浏览' in htitle else 'resources' if '资源' in htitle
                    else 'horizon' if '视野拓展' in htitle else 'leftovers' if '未推荐' in htitle
                    else 'sec-' + re.sub(r'[^\w-]', '', htitle))
            out.append(f'<section id="{slug}"><h2>{esc(htitle)}</h2>\n' + render_markdown_block(body, url_to_ref, esc) + '</section>')

    out += [build_like_html(refs), '</main><script>' + JS + '</script></body></html>']
    return '\n'.join(out)


def write(brief, candidate, target):
    Path(target).write_text(render(brief, candidate))

