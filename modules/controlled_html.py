"""Deterministic offline presentation. Untrusted inference never supplies executable CSS/JS."""
import html
import json
import re
from pathlib import Path
from modules.region import normalized

CSS = '''*{box-sizing:border-box;min-width:0}html,body{margin:0;width:100%;font-family:system-ui,sans-serif;color:#18222c;background:#f4f5f7;font-size:18px;line-height:1.8}main{max-width:900px;margin:auto;padding:16px}article,section,nav{background:white;border-radius:12px;padding:16px;margin:16px 0}a,p,pre,code,li,details,h1,h2,h3{overflow-wrap:anywhere;word-break:break-word}pre{white-space:pre-wrap}button,select{font:inherit;max-width:100%;padding:8px}h1{font-size:28px}h2{font-size:24px}h3{font-size:22px}nav a{display:block}summary{cursor:pointer}article[hidden]{display:none}'''
JS = '''const cards=[...document.querySelectorAll('article')];const topic=document.querySelector('#topic'),region=document.querySelector('#region');function filter(){let count=0;for(const card of cards){const shown=(!topic.value||card.dataset.direction===topic.value)&&(!region.value||card.dataset.region===region.value);card.hidden=!shown;if(shown)count++;}document.querySelector('#count').textContent=count;for(const link of document.querySelectorAll('nav [data-target]')){link.hidden=document.querySelector('#'+link.dataset.target).hidden;}}topic.addEventListener('change',filter);region.addEventListener('change',filter);filter();'''
MARKER = '<!-- dailybrief-controlled-html:v1 -->'


def render(brief, candidate):
    from modules.html_report import canonical_articles, build_like_html, RUN_ID_RE
    import add_like
    text = Path(brief).read_text()
    payload = json.loads(Path(candidate).read_text())
    refs = add_like.parse_brief_refs(brief)
    esc = lambda s: html.escape(str(s), quote=True)
    articles = canonical_articles(text)
    pool = {c['url']: c for k in ('candidates', 'source_leftovers', 'horizon') for c in payload.get(k, [])}
    run_id = RUN_ID_RE.search(text).group(1)
    choices = sorted({pool.get(a['cites'][0][1], {}).get('direction') or '其他' for a in articles if len(a['cites']) == 1})
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>DailyBrief</title><style>'+CSS+'</style></head><body><main>',
           MARKER, '<!-- run_id: '+esc(run_id)+' -->', '<h1>DailyBrief '+esc(payload.get('window', {}).get('end', ''))+'</h1>',
           '<p>全部 '+str(len(articles))+' 篇；当前显示 <span id="count">'+str(len(articles))+'</span> 篇。</p>',
           '<label>主题 <select id="topic"><option value="">全部</option>'+''.join('<option>'+esc(c)+'</option>' for c in choices)+'</select></label>',
           '<label>地区 <select id="region"><option value="">全部</option><option value="cn">国内</option><option value="intl">国外</option><option value="other">其他来源</option></select></label>',
           '<nav aria-label="目录"><h2>目录</h2>']
    for a in articles:
        if len(a['cites']) != 1:
            raise ValueError('canonical 每篇必须唯一引用')
        n,u = a['cites'][0]
        out.append('<a href="#article-'+esc(n)+'" data-target="article-'+esc(n)+'">['+esc(n)+'] '+esc(a['title'])+'</a>')
    out.append('</nav><section id="deep"><h2>今日热门文章</h2>')
    for a in articles:
        n,u = a['cites'][0]; c = pool.get(u,{})
        out += ['<article id="article-'+esc(n)+'" data-ref="'+esc(n)+'" data-url="'+esc(u)+'" data-direction="'+esc(c.get('direction') or '其他')+'" data-region="'+esc(normalized(c.get('region')))+'">',
                '<h3 data-role="title">'+esc(a['title'])+'</h3>', '<p data-role="summary">'+esc(a.get('summary_text',a['summary']))+'</p>',
                '<a data-role="citation" href="'+esc(u)+'">['+esc(n)+']原文链接</a>',
                '<details><summary>日期与原文证据</summary><p>'+esc(c.get('publish_date',''))+' '+esc(c.get('date_source',''))+'</p><pre>'+esc(c.get('date_evidence',''))+'</pre><p>'+esc(u)+'</p></details></article>']
    out.append('</section>')
    # Keep the remaining canonical sections, including TLDR, leftovers and source-shortage disclosures.
    for section in re.split(r'\n(?=## )', text):
        if section.startswith(('## 今日热门文章', '## 👍 点赞')):
            continue
        if section.startswith('## 参考资料'):
            out.append('<section><h2>参考资料</h2><ul>'+''.join('<li>['+str(n)+'] <a href="'+esc(u)+'">'+esc(t)+'</a></li>' for n,(t,u) in sorted(refs.items()))+'</ul></section>')
        elif section.startswith('## '):
            heading, _, body = section.partition('\n')
            out.append('<section><h2>'+esc(heading[3:])+'</h2><pre>'+esc(body)+'</pre></section>')
        elif '**今日速览' in section:
            body = section[section.index('**今日速览'):]
            out.append('<section><h2>今日速览</h2><pre>'+esc(body)+'</pre></section>')
    out += [build_like_html(refs), '</main><script>'+JS+'</script></body></html>']
    return '\n'.join(out)


def write(brief, candidate, target):
    Path(target).write_text(render(brief,candidate))
