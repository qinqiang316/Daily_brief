"""Conservative stdlib HTML body extraction; menus never supply article length."""
import re
from html.parser import HTMLParser

BODY_CLASSES = {'article-content','entry-content','post-content','article-body',
                'article__body','rich_media_content','article_content','post_content',
                'p_mainnew'}  # Solidot's verified article-only container
SKIP = {'script','style','nav','footer','header','aside','form','svg','button'}
VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[]
        self.blocks=[]
        self.all=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        blocked=tag in SKIP or bool(self.stack and self.stack[-1]['blocked'])
        classes=set(((attrs.get('class') or '')+' '+(attrs.get('id') or '')).split())
        priority=(3 if classes & BODY_CLASSES or attrs.get('itemprop')=='articleBody'
                  else 2 if tag=='article' else 1 if tag=='main' else 0)
        frame={'tag':tag,'blocked':blocked,'priority':priority,'text':[]}
        if tag not in VOID:
            self.stack.append(frame)
            if priority and not blocked:
                self.blocks.append(frame)

    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index]['tag']==tag:
                del self.stack[index:]
                break

    def handle_data(self,data):
        if self.stack and self.stack[-1]['blocked']:
            return
        self.all.append(data)
        for frame in self.stack:
            if frame['priority']:
                frame['text'].append(data)


def extract(raw):
    parser=ArticleParser()
    parser.feed(raw)
    if parser.blocks:
        block=max(parser.blocks,key=lambda b:(b['priority'],len(' '.join(b['text']))))
        text=' '.join(block['text'])
    else:
        text=' '.join(parser.all)
    return re.sub(r'\s+',' ',text).strip()
