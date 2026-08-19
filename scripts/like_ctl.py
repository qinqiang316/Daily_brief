#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点赞服务按需控制：读简报时启动，读完关闭（2026-08-13 改，省电）。

用法:
  python3 like_ctl.py start [--idle-timeout 900]   # 启动（默认 15 分钟无点击自动退出）
  python3 like_ctl.py stop                          # 立即关闭
  python3 like_ctl.py status                        # 查看状态

配合 Hermes 会话指令使用：
  - 用户说"读简报" / "开始读简报" → 本脚本 start + 打开最新简报
  - 用户说"读完" / "关闭点赞"   → 本脚本 stop
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "like_server.py")
PID_FILE = "/tmp/like_server.pid"
LOG_FILE = "/tmp/like_server.log"
PORT = 8900
IDLE_TIMEOUT = 900


def _read_pid():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            return None
    return None


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_start(idle_timeout):
    pid = _read_pid()
    if _alive(pid):
        print("已在运行（pid %d，端口 %d）" % (pid, PORT))
        return 0
    if _port_open():
        print("端口 %d 已被占用（可能旧进程残留），先 stop 再 start" % PORT)
        return 1
    log = open(LOG_FILE, "a", encoding="utf-8")
    p = subprocess.Popen(
        [sys.executable, SERVER, "--port", str(PORT), "--idle-timeout", str(idle_timeout)],
        stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # 脱离会话，like_ctl 退出不影响服务
    )
    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))
    # 等端口就绪
    for _ in range(20):
        if _port_open():
            print("已启动（pid %d）http://127.0.0.1:%d —— 空闲 %d 秒自动退出"
                  % (p.pid, PORT, idle_timeout))
            print("读完简报说\"读完\"即可关闭；忘了也没事，15 分钟无点击自动关。")
            return 0
        time.sleep(0.3)
    print("启动失败：端口未就绪（看 %s）" % LOG_FILE)
    return 1


def cmd_stop():
    pid = _read_pid()
    if not _alive(pid):
        # 无 pid 文件时尝试按端口找进程
        if _port_open():
            print("服务在运行但 pid 文件丢失，请手动关闭端口 %d 的进程" % PORT)
            return 1
        print("未在运行。")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not _alive(pid):
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
            print("已关闭（pid %d）。" % pid)
            return 0
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    print("已强制关闭（pid %d）。" % pid)
    return 0


def cmd_status():
    pid = _read_pid()
    if _alive(pid):
        print("运行中（pid %d，端口 %d %s）" % (pid, PORT, "已监听" if _port_open() else "未监听"))
        return 0
    if _port_open():
        print("端口 %d 有进程监听，但 pid 文件不匹配" % PORT)
        return 0
    print("未运行（读简报时说\"读简报\"即可启动）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="点赞服务控制")
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT)
    args = ap.parse_args()
    if args.action == "start":
        return cmd_start(args.idle_timeout)
    if args.action == "stop":
        return cmd_stop()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
