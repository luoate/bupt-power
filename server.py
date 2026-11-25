#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电费监控控制台。

启动:  python server.py        然后打开打印出来的地址
监听地址由 config.json 的 web.host / web.port 决定，默认 0.0.0.0（局域网可访问）
"""

import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import monitor as core

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"

app = Flask(__name__, static_folder=None)
# 抄表要轮询 30 秒，配置/查询并发进来时不能互相阻塞
lock = threading.Lock()

# 楼栋树很少变，缓存 10 分钟，避免每次点选都登录一次
_tree_cache = {}
_TREE_TTL = 600

# 后台调度状态，供前端显示
SCHED_TICK = 60
sched = {
    "running": False,
    "lastRunAt": None,
    "lastStatus": None,
    "lastMessage": None,
}


def scheduler_loop():
    """常驻线程：每分钟醒一次，到点才真正请求，否则什么都不做。"""
    sched["running"] = True
    while True:
        time.sleep(SCHED_TICK)
        try:
            cfg, state = cfg_and_state()
            refresh_ok, _ = core.refresh_due(cfg, state)
            check_ok, _ = core.check_due(cfg, state)
            if not refresh_ok and not check_ok:
                continue
            # 手动刷新正在进行时让路，下一分钟再来
            if not lock.acquire(blocking=False):
                continue
            try:
                res = core.run_cycle(cfg, state, "auto")
            finally:
                lock.release()
            sched["lastRunAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sched["lastStatus"] = res["status"]
            sched["lastMessage"] = res.get("message")
        except Exception as e:
            sched["lastRunAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sched["lastStatus"] = "error"
            sched["lastMessage"] = str(e)


def cfg_and_state():
    cfg = core.load_config(CONFIG_PATH)
    state = core.load_state(ROOT / cfg.get("state_file", "state.json"))
    return cfg, state


def meter_payload(m, cfg, state):
    return {
        "balance": m.get("leftMoney"),
        "meterData": m.get("meterData"),
        "price": m.get("price"),
        "remaining": m.get("remaining"),
        "preUseDay": m.get("preUseDay"),
        "onlineStatus": m.get("onlineStatus"),
        "meterStatus": m.get("meterStatus"),
        "updateDateTime": m.get("updateDateTime"),
        "quota": m.get("dayDCTimes"),
        "room": cfg["room"]["label"],
        "threshold": cfg.get("threshold_yuan"),
        "alerting": bool(state.get("alerting")),
        "nextRefreshIn": core.next_refresh_eta(cfg, state),
        "nextCheckIn": core.next_check_eta(cfg, state),
        "checkIntervalMinutes": (cfg.get("site") or {}).get("check_interval_minutes", 60),
        "lastRefreshAt": state.get("last_refresh_at"),
        "lastRefreshResult": state.get("last_refresh_result"),
        "history": core.read_history(cfg, 60),
        "scheduler": dict(sched),
    }


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/app.css")
def css():
    return send_from_directory(WEB_DIR, "app.css")


@app.get("/app.js")
def js():
    return send_from_directory(WEB_DIR, "app.js")


@app.get("/api/buildings")
def api_buildings():
    parent = request.args.get("parent") or "001"
    now = time.time()
    hit = _tree_cache.get(parent)
    if hit and now - hit[0] < _TREE_TTL:
        return jsonify(ok=True, parent=parent, items=hit[1])

    cfg, _ = cfg_and_state()
    try:
        items = core.fetch_buildings(core.make_session(), cfg["site"], parent)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502
    _tree_cache[parent] = (now, items)
    return jsonify(ok=True, parent=parent, items=items)


@app.get("/api/status")
def api_status():
    cfg, state = cfg_and_state()
    try:
        m = core.pick_electric(core.fetch_meters(core.make_session(), cfg["site"], cfg["room"]["building_id"]))
    except Exception as e:
        return jsonify(ok=False, error=str(e), history=core.read_history(cfg, 60),
                       nextRefreshIn=core.next_refresh_eta(cfg, state)), 502
    return jsonify(ok=True, **meter_payload(m, cfg, state))


@app.post("/api/refresh")
def api_refresh():
    if not lock.acquire(blocking=False):
        return jsonify(ok=False, error="已有一次刷新正在进行"), 429
    try:
        cfg, state = cfg_and_state()
        session = core.make_session()
        m = core.pick_electric(core.fetch_meters(session, cfg["site"], cfg["room"]["building_id"]))

        quota = m.get("dayDCTimes")
        if isinstance(quota, int) and quota <= 0:
            return jsonify(ok=False, error="今日刷新额度已用完，明天零点恢复",
                           **meter_payload(m, cfg, state)), 429

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["last_refresh_attempt"] = now_str
        try:
            result = core.trigger_refresh(session, cfg["site"], cfg["room"]["building_id"], m.get("comAddress"))
        except Exception as e:
            result = f"error: {e}"
        state["last_refresh_result"] = result

        if result != "ok":
            state["refresh_fail_count"] = state.get("refresh_fail_count", 0) + 1
            core.save_state(ROOT / cfg.get("state_file", "state.json"), state)
            return jsonify(ok=False, error=f"抄表未成功：{result}", **meter_payload(m, cfg, state)), 502

        state["last_refresh_at"] = now_str
        state["refresh_fail_count"] = 0
        m = core.pick_electric(core.fetch_meters(session, cfg["site"], cfg["room"]["building_id"]))
        core.append_history(cfg, m, "强制抄表")
        core.save_state(ROOT / cfg.get("state_file", "state.json"), state)
        return jsonify(ok=True, **meter_payload(m, cfg, state))
    finally:
        lock.release()


@app.get("/api/config")
def api_get_config():
    cfg, _ = cfg_and_state()
    smtp = cfg.get("smtp") or {}
    return jsonify({
        "room": cfg.get("room", {}),
        "threshold_yuan": cfg.get("threshold_yuan"),
        "alert": cfg.get("alert", {}),
        "check_interval_minutes": (cfg.get("site") or {}).get("check_interval_minutes", 60),
        "refresh": cfg.get("refresh", {}),
        "smtp": {
            "host": smtp.get("host", ""),
            "port": smtp.get("port", 465),
            "ssl": smtp.get("ssl", True),
            "username": smtp.get("username", ""),
            "sender": smtp.get("sender", ""),
            "recipients": smtp.get("recipients", []),
            # 密码永不回传，只用标记位告诉前端「已设置」
            "has_password": bool(smtp.get("password")),
        },
    })


@app.post("/api/config")
def api_save_config():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return jsonify(ok=False, error="请求体为空或格式不正确"), 400
    cfg, _ = cfg_and_state()

    # 支持局部更新：只提交宿舍时不要求带阈值等其他字段
    if "threshold_yuan" in payload:
        try:
            threshold = float(payload["threshold_yuan"])
        except (TypeError, ValueError):
            return jsonify(ok=False, error="告警阈值必须是数字"), 400
        if threshold <= 0:
            return jsonify(ok=False, error="告警阈值必须大于 0"), 400
        cfg["threshold_yuan"] = threshold

    room_in = payload.get("room") or {}
    if room_in.get("building_id"):
        room_id = str(room_in["building_id"]).strip()
        if len(room_id) != 24:
            return jsonify(ok=False, error="房间编号不合法，请重新选择宿舍"), 400
        cfg["room"] = {
            "label": str(room_in.get("label") or room_id).strip(),
            "building_id": room_id,
        }

    if "check_interval_minutes" in payload:
        try:
            check_min = float(payload["check_interval_minutes"])
        except (TypeError, ValueError):
            return jsonify(ok=False, error="读取余额间隔必须是数字"), 400
        if not 10 <= check_min <= 1440:
            return jsonify(ok=False, error="读取余额间隔需在 10 到 1440 分钟之间"), 400
        cfg.setdefault("site", {})["check_interval_minutes"] = check_min

    if "cooldown_hours" in (payload.get("alert") or {}):
        try:
            cooldown = float(payload["alert"]["cooldown_hours"])
        except (TypeError, ValueError):
            return jsonify(ok=False, error="邮件冷却时间必须是数字"), 400
        if not 1 <= cooldown <= 720:
            return jsonify(ok=False, error="邮件冷却时间需在 1 到 720 小时之间"), 400
        cfg.setdefault("alert", {})["cooldown_hours"] = cooldown

    refresh = payload.get("refresh") or {}
    rcfg = cfg.setdefault("refresh", {})
    rcfg["enabled"] = bool(refresh.get("enabled", rcfg.get("enabled", True)))
    try:
        hours = float(refresh.get("interval_hours", rcfg.get("interval_hours", 12)))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="抄表间隔必须是数字"), 400
    if not 1 <= hours <= 24:
        return jsonify(ok=False, error="抄表间隔需在 1 到 24 小时之间"), 400
    rcfg["interval_hours"] = hours

    smtp_in = payload.get("smtp") or {}
    smtp = cfg.setdefault("smtp", {})
    for key in ("host", "username", "sender"):
        if smtp_in.get(key) is not None:
            smtp[key] = str(smtp_in[key]).strip()
    try:
        port = int(smtp_in.get("port", smtp.get("port", 465)))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="SMTP 端口必须是数字"), 400
    smtp["port"] = port
    smtp["ssl"] = bool(smtp_in.get("ssl", smtp.get("ssl", True)))

    new_pw = (smtp_in.get("password") or "").strip()
    if new_pw:  # 留空表示沿用已保存的授权码
        smtp["password"] = new_pw

    recips = smtp_in.get("recipients")
    if recips is not None:
        smtp["recipients"] = core.parse_recipients(recips)

    core.save_config(CONFIG_PATH, cfg)
    return jsonify(ok=True)


@app.post("/api/test-email")
def api_test_email():
    payload = request.get_json(silent=True) or {}
    cfg, _ = cfg_and_state()
    if payload.get("smtp"):
        # 允许未保存就测试：把表单值临时合并进来
        tmp = {"smtp": {**cfg.get("smtp", {}), **{k: v for k, v in payload["smtp"].items() if v not in (None, "")}}}
        if not tmp["smtp"].get("password"):
            tmp["smtp"]["password"] = (cfg.get("smtp") or {}).get("password", "")
        cfg = {**cfg, **tmp}
    try:
        core.send_mail(cfg["smtp"], "【电费监控】测试邮件",
                       f"SMTP 配置可用。\n发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        return jsonify(ok=False, error=smtp_error_text(e)), 502
    return jsonify(ok=True)


def smtp_error_text(e):
    """把 SMTP 的原始异常翻译成能照着改的提示。"""
    msg = str(e)
    low = msg.lower()
    if isinstance(e, ValueError):
        return msg
    if "ascii" in low and "codec" in low:
        return "邮箱地址含有中文或非法字符，请填写真实邮箱地址"
    if "535" in msg or "authentication" in low or "auth" in low:
        return "SMTP 认证失败：请确认邮箱地址与授权码正确，且已开启 SMTP 服务"
    if "timed out" in low or "timeout" in low:
        return "连接 SMTP 服务器超时：请检查服务器地址与端口（SSL 通常用 465）"
    if "getaddrinfo" in low or "name or service not known" in low:
        return "无法解析 SMTP 服务器地址，请检查拼写"
    if "certificate" in low or "ssl" in low:
        return "SSL 握手失败：若服务器使用 587 端口，请把加密方式改为 STARTTLS"
    if "connection refused" in low or "network is unreachable" in low:
        return "无法连接到 SMTP 服务器：请检查网络、端口或被防火墙拦截"
    return msg


def lan_addresses():
    """列出物理网卡上的 IPv4 地址，排除 Clash/WSL/Hyper-V 之类的虚拟网卡。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object { -not $_.Virtual -and $_.Status -eq 'Up' } | "
             "ForEach-Object { Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 "
             "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress }"],
            capture_output=True, text=True, timeout=15,
        )
        ips = [line.strip() for line in out.stdout.splitlines()]
        ips = [ip for ip in ips if ip and not ip.startswith(("127.", "169.254."))]
        if ips:
            return ips
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                if not ip.startswith(("127.", "169.254."))]
    except OSError:
        return []


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    boot_cfg = core.load_config(CONFIG_PATH)
    web_cfg = boot_cfg.setdefault("web", {})
    host = web_cfg.get("host") or "0.0.0.0"
    port = int(web_cfg.get("port") or 5000)

    core.setup_logging(boot_cfg)
    # 服务常驻期间由自己承担定时监控，不依赖 Windows 计划任务
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()

    print("=" * 46)
    print("电费监控控制台已启动")
    print(f"  本机访问   http://127.0.0.1:{port}")
    if host in ("0.0.0.0", ""):
        for ip in lan_addresses():
            print(f"  局域网访问 http://{ip}:{port}")
        print("  （手机连同一个 WiFi，用上面的局域网地址）")
    else:
        print(f"  仅绑定了 {host}，局域网内无法访问")
    print("=" * 46)

    app.run(host=host, port=port, debug=False, threaded=True)
