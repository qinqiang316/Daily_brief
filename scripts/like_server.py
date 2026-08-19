#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简报点赞 HTTP 服务：点击简报里的点赞链接即记录点赞（2026-08-13 新增）。

用法:
  python3 like_server.py [--port 8900] [--host 127.0.0.1] [--idle-timeout 900]

路由:
  GET /like?url=<urlencoded>&title=<title>   记录点赞，返回确认页
  GET /likes                                 点赞列表（HTML）
  GET /stats                                 点赞统计（HTML）
  GET /                                      说明页

运行模式（2026-08-13 改，省电）:
  不再 launchd 常驻。按需启动 + 空闲自动退出：
    - 读简报时用 like_ctl.py start 启动（或 Hermes 会话说"读简报"）
    - 默认 15 分钟（--idle-timeout 秒）无任何点击自动退出，读完忘关也不费电
    - 也可 like_ctl.py stop 手动关闭
限制: 仅监听 127.0.0.1（本机点击有效）；手机端请用"点赞 N"文字指令。
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import likes as likes_mod
from add_like import add_like

PORT = 8900
HOST = "127.0.0.1"
IDLE_TIMEOUT = 900  # 15 分钟无点击自动退出
HTTP_RE = re.compile(r"^https?://", re.I)

_last_activity = time.time()


def _page(title, body):
    return ("<!DOCTYPE html><html lang=zh-CN><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title>"
            "<body style='font-family:-apple-system,sans-serif;max-width:520px;"
            "margin:40px auto;padding:0 16px;line-height:1.7'>"
            "<h2>%s</h2>%s</body></html>") % (html.escape(title), html.escape(title), body)


class LikeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[like] %s\n" % (fmt % args))

    def do_GET(self):
        global _last_activity
        _last_activity = time.time()
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/like":
            self.handle_like(qs)
        elif parsed.path == "/likes":
            self.handle_list()
        elif parsed.path == "/stats":
            self.handle_stats()
        elif parsed.path in ("/", "/favicon.ico"):
            self.handle_home()
        else:
            self.send_error(404, "Not Found")

    def handle_like(self, qs):
        url = (qs.get("url") or [""])[0].strip()
        title = (qs.get("title") or [""])[0].strip()[:120]
        if not HTTP_RE.match(url) or len(url) > 500:
            self._send(_page("链接无效", "<p>链接参数无效，请从简报里点击原文点赞链接。</p>"), 400)
            return
        try:
            ok, msg, dup = add_like(url, title)
        except Exception as e:
            self._send(_page("记录失败", "<p>写入点赞失败：%s</p>" % html.escape(str(e))), 500)
            return
        likes = likes_mod.load_likes()
        pref, total, min_total, top, top_cnt, share = likes_mod.pref_progress(likes)
        explore = likes_mod.explore_direction(pref)
        body = ["<p>%s</p>" % html.escape(msg)]
        if ok:
            if pref:
                body.append("<p>当前偏好：%s —— 明天简报会优先更多这类内容</p>" % pref)
            elif total < min_total:
                body.append("<p>偏好积累中：已点赞 %d/%d 条（还差 %d 条启用偏好优先采集）</p>"
                            % (total, min_total, min_total - total))
            else:
                body.append("<p>偏好待定：%d 条中 %s 占 %d%%（< %d%%），方向不够集中</p>"
                            % (total, top, round(share * 100), round(likes_mod.MIN_PREF_SHARE * 100)))
            body.append("<p>今日探索方向：%s —— 每天仍会给你 1-2 篇新方向的探索</p>" % explore)
        body.append('<p style="color:#888;font-size:13px">'
                    '<a href="/likes">查看全部点赞</a> · '
                    '<a href="/stats">偏好统计</a></p>')
        self._send(_page("点赞成功" if ok else "已点过", "".join(body)))

    def handle_list(self):
        likes = likes_mod.load_likes()
        if not likes:
            body = "<p>还没有点赞记录。从简报最下方的点赞区点链接即可。</p>"
        else:
            rows = ["<ul>"]
            for x in likes:
                rows.append("<li>[%s] %s <br><span style='color:#888;font-size:12px'>%s（%s）</span></li>"
                            % (html.escape(x.get("direction", "?")),
                               html.escape(x.get("title", "")),
                               html.escape(x.get("url", "")),
                               html.escape((x.get("liked_at") or "")[:10])))
            rows.append("</ul>")
            body = "".join(rows)
        self._send(_page("点赞列表（%d 条）" % len(likes), body))

    def handle_stats(self):
        likes = likes_mod.load_likes()
        from collections import Counter
        cnt = Counter(x.get("direction", "?") for x in likes)
        pref, total, min_total, top, top_cnt, share = likes_mod.pref_progress(likes)
        explore = likes_mod.explore_direction(pref)
        rows = ["<p>点赞总数：<b>%d</b></p>" % len(likes)]
        for d in likes_mod.DIRECTIONS:
            rows.append("<p>%s：%d</p>" % (d, cnt.get(d, 0)))
        if pref:
            rows.append("<p><b>当前偏好方向：%s</b>（%d 条中占 %d%% ≥ %d%%，已启用偏好优先采集）</p>"
                        % (pref, total, round(share * 100), round(likes_mod.MIN_PREF_SHARE * 100)))
        elif total < min_total:
            rows.append("<p>偏好方向：<b>样本积累中</b>（%d/%d 条，还差 %d 条启用偏好优先采集）</p>"
                        % (total, min_total, min_total - total))
        elif top:
            rows.append("<p>偏好方向：<b>待定</b>（%d 条中 %s 占 %d%% < %d%% 下限，方向不够集中）</p>"
                        % (total, top, round(share * 100), round(likes_mod.MIN_PREF_SHARE * 100)))
        rows.append("<p><b>今日探索方向：%s</b>（样本不足阶段照常轮换探索）</p>" % explore)
        self._send(_page("偏好统计", "".join(rows)))

    def handle_home(self):
        body = ("<p>这是简报点赞服务。使用方式：</p>"
                "<ol><li>打开任意一份 <b>Daily-Brief-*.md</b> 简报文件</li>"
                "<li>拉到最下方「👍 点赞」区，点某篇文章的点赞链接</li>"
                "<li>自动记录，明天简报会优先推荐类似内容</li></ol>"
                '<p><a href="/likes">查看点赞列表</a> · '
                '<a href="/stats">偏好统计</a></p>')
        self._send(_page("简报点赞服务", body))

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global IDLE_TIMEOUT
    ap = argparse.ArgumentParser(description="简报点赞 HTTP 服务")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT,
                    help="空闲自动退出秒数（默认 900=15 分钟；0=不退出）")
    args = ap.parse_args()
    IDLE_TIMEOUT = args.idle_timeout
    srv = HTTPServer((args.host, args.port), LikeHandler)
    srv.timeout = 1.0  # handle_request 轮询间隔，用于空闲检测
    sys.stderr.write("[like] 点赞服务运行于 http://%s:%d（空闲 %d 秒自动退出）\n"
                     % (args.host, args.port, IDLE_TIMEOUT))
    try:
        if IDLE_TIMEOUT <= 0:
            srv.serve_forever()
        else:
            while True:
                srv.handle_request()
                if time.time() - _last_activity > IDLE_TIMEOUT:
                    sys.stderr.write("[like] 空闲超过 %d 秒，自动退出（读简报时说\"读简报\"即可再启动）\n"
                                     % IDLE_TIMEOUT)
                    break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
