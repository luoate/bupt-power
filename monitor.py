#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BUPT 沙河校区宿舍电费监控：每 12 小时主动抄表，余额低于阈值时发邮件预警。

用法:
    python monitor.py --check        只读取缓存余额，不消耗刷新额度
    python monitor.py                到点则强制抄表 + 按阈值发预警（供计划任务调用）
    python monitor.py --refresh      立刻强制抄表一次（会消耗一次刷新额度）
    python monitor.py --loop         常驻运行，由本进程自己按间隔调度
    python monitor.py --test-email   发一封测试邮件，验证 SMTP 配置
"""

import argparse
import csv
import hashlib
import json
import logging
import random
import re
import shutil
import smtplib
import ssl
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
BASE_URL = "https://shgyfk.bupt.edu.cn/APIXCX"
# 站点前端内置的查询账号（页面 bindroom 里硬编码，无需学号密码）
SITE_USER = "szbdyb"
SITE_KEY = "4arcOUm6Wau+VuBX8g+IPg=="
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1")

log = logging.getLogger("electricity")

ADDRESS_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def load_config(path):
    path = Path(path)
    if not path.exists():
        # 首次运行：从仓库里的模板复制一份，用户再在网页上填自己的信息
        example = path.with_name("config.example.json")
        if example.exists():
            shutil.copyfile(example, path)
            log.warning("未找到 %s，已从 config.example.json 复制一份", path.name)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("site", {})
    cfg["site"].setdefault("base_url", BASE_URL)
    cfg["site"].setdefault("user", SITE_USER)
    cfg["site"].setdefault("key", SITE_KEY)
    return cfg


def setup_logging(cfg):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handler = logging.FileHandler(ROOT / cfg.get("log_file", "monitor.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr is not None:  # pythonw 下没有控制台
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)
    log.setLevel(logging.INFO)


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Origin": "https://shgyfk.bupt.edu.cn",
        "Referer": "https://shgyfk.bupt.edu.cn/h5/bindroom",
    })
    return s


def login(session, site):
    t = int(time.time())
    num = str(random.randint(1000, 9999))
    sign = hashlib.sha256(f"userName={site['user']}&time={t}&num={num}&key={site['key']}".encode()).hexdigest()
    r = session.post(f"{site['base_url']}/app/xcx/login",
                     data={"userName": site["user"], "time": t, "num": num, "sign": sign},
                     headers={"content-type": "application/x-www-form-urlencoded"}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"登录失败: {body.get('msg')}")
    return body["token"]


def fetch_meters(session, site, building_id):
    token = login(session, site)
    r = session.get(f"{site['base_url']}/app/xcx/meterinfo/getMeterStatus1",
                    params={"token": token, "buildingID": building_id}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("code") == 401:  # token 过期，重登一次
        token = login(session, site)
        r = session.get(f"{site['base_url']}/app/xcx/meterinfo/getMeterStatus1",
                        params={"token": token, "buildingID": building_id}, timeout=20)
        body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"查询失败: {body.get('msg')}")
    return body.get("data") or []


def pick_electric(meters):
    for m in meters:
        if m.get("type") == "电表":
            return m
    if meters:
        return meters[0]
    raise RuntimeError("该房间没有返回任何表计")


def fetch_buildings(session, site, parent_id):
    """读某一层级下的子节点。叶子（房间）返回空列表。"""
    token = login(session, site)
    r = session.get(f"{site['base_url']}/app/meterinfo/getbuildinginfo1",
                    params={"token": token, "buildingID": parent_id}, timeout=20)
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"读取楼栋失败: {body.get('msg')}")
    return [{"id": b.get("buildingID"), "name": b.get("buildingName")} for b in (body.get("data") or [])]


def trigger_refresh(session, site, building_id, com_addr):
    """下发一次抄表指令并等待结果。返回 ok / failed / timeout。"""
    token = login(session, site)
    r = session.get(f"{site['base_url']}/ypt/app/message/readMeter",
                    params={"token": token, "phone": "微信用户", "buildingID": building_id,
                            "comAddress": com_addr, "dataItemID": "00057", "userName": "微信用户"},
                    timeout=20)
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"下发抄表任务失败: {body.get('msg')}")
    task_id = body.get("taskID") or (body.get("data") or {}).get("taskID")
    if not task_id:
        raise RuntimeError(f"抄表任务未返回 taskID: {r.text[:200]}")

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(2)
        try:
            tb = session.get(f"{site['base_url']}/ypt/app/message/getreadMeter",
                             params={"token": token, "phone": "微信用户", "taskID": task_id},
                             timeout=20).json()
        except Exception:
            continue
        if tb.get("code") != 0:
            continue
        status = (tb.get("data") or {}).get("readStatus")
        if status == 1:
            return "ok"
        if status == 2:
            return "failed"
    return "timeout"


def check_due(cfg, state):
    """是否到了下一次读取缓存余额的时间。"""
    interval = float((cfg.get("site") or {}).get("check_interval_minutes", 60)) * 60
    last = state.get("last_check_at")
    if not last:
        return True, "首次运行"
    try:
        ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return True, "时间记录异常"
    left = interval - (time.time() - ts)
    if left <= 0:
        return True, "已到间隔"
    return False, f"距下次读余额 {int(left // 60)} 分"


def next_check_eta(cfg, state):
    """距下次读取缓存余额的秒数；0 表示已到期。"""
    interval = float((cfg.get("site") or {}).get("check_interval_minutes", 60)) * 60
    last = state.get("last_check_at")
    if not last:
        return 0
    try:
        ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0
    return max(0, int(interval - (time.time() - ts)))


def alert_ready(cfg, state):
    """距上次预警邮件是否已超过冷却时间。"""
    cooldown = float((cfg.get("alert") or {}).get("cooldown_hours", 24)) * 3600
    last = state.get("last_alert_at")
    if not last:
        return True
    try:
        ts = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return True
    return (time.time() - ts) >= cooldown


def refresh_due(cfg, state):
    """是否到了下一次强制抄表的时间。失败后只等 retry_after_minutes，避免烧光额度。"""
    rcfg = cfg.get("refresh") or {}
    if not rcfg.get("enabled", False):
        return False, "未启用强制抄表"
    interval = float(rcfg.get("interval_hours", 12)) * 3600
    retry = float(rcfg.get("retry_after_minutes", 60)) * 60

    if state.get("last_refresh_result") == "ok" and state.get("last_refresh_at"):
        base, window = state["last_refresh_at"], interval
    elif state.get("last_refresh_attempt"):
        base, window = state["last_refresh_attempt"], retry
    else:
        return True, "首次运行"

    try:
        ts = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return True, "时间记录异常"
    left = window - (time.time() - ts)
    if left <= 0:
        return True, "已到间隔"
    hours, mins = divmod(int(left // 60), 60)
    return False, f"距下次抄表 {hours} 小时 {mins} 分"


def parse_decimal(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


HISTORY_HEADER = ["查询时间", "余额(元)", "表读数", "单价", "剩余电量", "预估可用天数", "表更新时间", "在线状态", "数据来源"]


def append_history(cfg, m, source):
    path = ROOT / cfg.get("history_file", "history.csv")
    rows = []
    if path.exists():
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    if rows and rows[0] != HISTORY_HEADER:  # 旧表头补一列，避免错位
        rows = [HISTORY_HEADER] + [r + [""] * (len(HISTORY_HEADER) - len(r)) for r in rows[1:]]
    elif not rows:
        rows = [HISTORY_HEADER]

    rows.append([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), m.get("leftMoney"), m.get("meterData"),
                 m.get("price"), m.get("remaining"), m.get("preUseDay"), m.get("updateDateTime"),
                 m.get("onlineStatus"), source])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


def parse_recipients(value):
    """收件人允许传列表，也允许传逗号分隔字符串。"""
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    out = []
    for item in (value or []):
        if item is None:
            continue
        addr = str(item).strip()
        if addr:
            out.append(addr)
    return out


def send_mail(smtp_cfg, subject, body):
    sender = (smtp_cfg.get("sender") or smtp_cfg.get("username") or "").strip()
    recipients = parse_recipients(smtp_cfg.get("recipients"))

    if not recipients:
        raise ValueError("尚未设置收件人邮箱")
    if not sender:
        raise ValueError("尚未设置发件邮箱")
    for addr in [sender] + recipients:
        if not addr.isascii() or not ADDRESS_RE.fullmatch(addr):
            raise ValueError(f"邮箱地址格式不正确：{addr}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    if smtp_cfg.get("ssl", True):
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], context=ctx, timeout=30) as s:
            s.login(smtp_cfg["username"], smtp_cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(smtp_cfg["username"], smtp_cfg["password"])
            s.send_message(msg)


def describe(m, label, source="缓存"):
    bal = parse_decimal(m.get("leftMoney"))
    return (
        f"房间：{label}\n"
        f"当前余额：{bal} 元\n"
        f"电价：{m.get('price')} 元/度\n"
        f"表读数：{m.get('meterData')} 度\n"
        f"剩余电量：{m.get('remaining')} 度\n"
        f"预估可用：{m.get('preUseDay')} 天\n"
        f"数据来源：{source}\n"
        f"今日剩余刷新次数：{m.get('dayDCTimes')}\n"
        f"表计状态：{m.get('onlineStatus')} {m.get('meterStatus') or ''}\n"
        f"数据更新时间：{m.get('updateDateTime')}\n"
        f"充值入口：https://shgyfk.bupt.edu.cn/h5/bindroom\n"
    )


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def save_config(path, cfg):
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def read_history(cfg, limit=100):
    path = ROOT / cfg.get("history_file", "history.csv")
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in body[-limit:]][::-1]


def next_refresh_eta(cfg, state):
    """距下次自动抄表的秒数；None 表示已到期或未启用。"""
    rcfg = cfg.get("refresh") or {}
    if not rcfg.get("enabled", False):
        return None
    if state.get("last_refresh_result") == "ok" and state.get("last_refresh_at"):
        base, window = state["last_refresh_at"], float(rcfg.get("interval_hours", 12)) * 3600
    elif state.get("last_refresh_attempt"):
        base, window = state["last_refresh_attempt"], float(rcfg.get("retry_after_minutes", 60)) * 60
    else:
        return 0
    try:
        ts = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0
    return max(0, int(window - (time.time() - ts)))


def notify(cfg, m, state, today, source="缓存"):
    threshold = Decimal(str(cfg["threshold_yuan"]))
    balance = parse_decimal(m.get("leftMoney"))
    if balance is None:
        raise RuntimeError("接口返回的余额无法解析")

    label = cfg["room"]["label"]
    below = balance < threshold
    kind = None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if below and alert_ready(cfg, state):
        send_mail(cfg["smtp"], f"【电费预警】{label} 余额仅 {balance} 元",
                  f"宿舍电费已低于 {threshold} 元，请及时充值，避免停电。\n\n{describe(m, label, source)}")
        state["last_alert_at"] = now_str
        kind = "alert"
    elif not below and state.get("alerting"):
        send_mail(cfg["smtp"], f"【电费恢复】{label} 余额已回到 {balance} 元",
                  f"余额已高于 {threshold} 元，预警解除。\n\n{describe(m, label, source)}")
        state["last_alert_at"] = None
        kind = "recovery"

    state["alerting"] = below
    state["last_balance"] = str(balance)
    state["last_success_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return kind


def run_cycle(cfg, state, mode="auto"):
    """跑一轮监控：读缓存 → 按需强制抄表 → 记历史 → 判断发信。

    mode: auto 按配置间隔决定是否抄表 / force 立刻抄表 / check 只读缓存。
    state 由本函数负责落盘，调用方不要再写一次。
    返回 {"status", "message", "meter", "source", "notified"}；
    status 为 error / ok / mail_error。
    """
    state_path = ROOT / cfg.get("state_file", "state.json")
    session = make_session()

    try:
        m = pick_electric(fetch_meters(session, cfg["site"], cfg["room"]["building_id"]))
    except Exception as e:
        log.error("查询失败: %s", e)
        if mode != "check":
            state["fail_count"] = state.get("fail_count", 0) + 1
            today = date.today().isoformat()
            if state["fail_count"] >= 3 and state.get("last_error_date") != today:
                try:
                    send_mail(cfg["smtp"], "【电费监控】连续查询失败",
                              f"电费监控已连续 {state['fail_count']} 次查询失败，请检查。\n最后错误：{e}")
                    state["last_error_date"] = today
                except Exception as mail_err:
                    log.error("错误通知发送失败: %s", mail_err)
            save_state(state_path, state)
        return {"status": "error", "message": str(e), "meter": None, "source": None, "notified": None}

    source = "缓存"
    if mode != "check":
        due, reason = (True, "手动指定") if mode == "force" else refresh_due(cfg, state)
        if due and (cfg.get("refresh") or {}).get("enabled", False):
            quota = m.get("dayDCTimes")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(quota, int) and quota <= 0:
                log.warning("今日刷新额度已用完，本轮改用缓存数据")
                state["last_refresh_result"] = "no_quota"
                state["last_refresh_attempt"] = now_str
            else:
                log.info("开始强制抄表（%s），剩余额度 %s", reason, quota)
                try:
                    result = trigger_refresh(session, cfg["site"], cfg["room"]["building_id"],
                                             m.get("comAddress"))
                except Exception as e:
                    result = f"error: {e}"
                state["last_refresh_attempt"] = now_str
                state["last_refresh_result"] = result
                if result == "ok":
                    state["last_refresh_at"] = now_str
                    state["refresh_fail_count"] = 0
                    m = pick_electric(fetch_meters(session, cfg["site"], cfg["room"]["building_id"]))
                    source = "强制抄表"
                    log.info("抄表成功，余额 %s 元（更新于 %s）", m.get("leftMoney"), m.get("updateDateTime"))
                else:
                    state["refresh_fail_count"] = state.get("refresh_fail_count", 0) + 1
                    log.warning("抄表未成功: %s（连续 %s 次）", result, state["refresh_fail_count"])
        elif due:
            log.info("强制抄表未启用，使用缓存数据")
        else:
            log.info("%s，使用缓存数据", reason)

    state["fail_count"] = 0
    state["last_check_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("余额 %s 元（更新于 %s）", m.get("leftMoney"), m.get("updateDateTime"))

    if mode == "check":
        save_state(state_path, state)
        return {"status": "ok", "message": f"余额 {m.get('leftMoney')} 元",
                "meter": m, "source": source, "notified": None}

    append_history(cfg, m, source)

    today = date.today().isoformat()
    if state.get("refresh_fail_count", 0) >= 3 and state.get("last_refresh_alert_date") != today:
        try:
            send_mail(cfg["smtp"], "【电费监控】强制抄表连续失败",
                      f"已连续 {state['refresh_fail_count']} 次强制抄表失败，当前显示的是缓存数据，"
                      f"可能已经不准。\n最后一次错误：{state.get('last_refresh_result')}\n\n"
                      + describe(m, cfg["room"]["label"], source))
            state["last_refresh_alert_date"] = today
        except Exception as e:
            log.error("抄表失败通知发送失败: %s", e)

    notified, status = None, "ok"
    try:
        notified = notify(cfg, m, state, today, source)
        if notified:
            log.info("已发送%s邮件", "预警" if notified == "alert" else "恢复")
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        status = "mail_error"
    finally:
        save_state(state_path, state)

    return {"status": status, "message": f"余额 {m.get('leftMoney')} 元（{source}）",
            "meter": m, "source": source, "notified": notified}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--check", action="store_true", help="只读取缓存余额，不消耗刷新额度")
    ap.add_argument("--refresh", action="store_true", help="立刻强制抄表一次（消耗一次额度）")
    ap.add_argument("--test-email", action="store_true", help="发送测试邮件")
    ap.add_argument("--loop", action="store_true", help="常驻运行，按配置间隔自动监控")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    if args.test_email:
        send_mail(cfg["smtp"], "【电费监控】测试邮件", "SMTP 配置可用，监控脚本已就绪。")
        log.info("测试邮件已发送")
        return 0

    if args.loop:
        return run_forever(args.config)

    state_path = ROOT / cfg.get("state_file", "state.json")
    state = load_state(state_path)

    mode = "force" if args.refresh else ("check" if args.check else "auto")

    # 外部计划任务跑得比配置的间隔勤，没到点就直接退出，避免无谓请求
    if mode == "auto":
        refresh_ok, _ = refresh_due(cfg, state)
        check_ok, check_reason = check_due(cfg, state)
        if not refresh_ok and not check_ok:
            log.info("本轮跳过（%s）", check_reason)
            return 0

    res = run_cycle(cfg, state, mode)
    if res["meter"] is None:
        return 1
    if sys.stdout is not None:
        print(describe(res["meter"], cfg["room"]["label"], res["source"] or "缓存"))
    return 1 if res["status"] == "mail_error" else 0


def run_forever(config_path, tick=60):
    """常驻循环：每分钟醒来一次，到点才真正请求。"""
    log.info("监控循环启动，每 %s 秒检查一次", tick)
    while True:
        try:
            cfg = load_config(config_path)
            state = load_state(ROOT / cfg.get("state_file", "state.json"))
            refresh_ok, _ = refresh_due(cfg, state)
            check_ok, _ = check_due(cfg, state)
            if refresh_ok or check_ok:
                run_cycle(cfg, state, "auto")
        except Exception as e:
            log.error("监控循环异常: %s", e)
        time.sleep(tick)


if __name__ == "__main__":
    sys.exit(main())
