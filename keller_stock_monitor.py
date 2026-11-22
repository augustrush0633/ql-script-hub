#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
cron "0 0 1 1 *" script-path=keller_stock_monitor.py,tag=匹配cron用
new Env('Keller 商品库存监控')

监控 kellerww.de 商品页库存状态：
    页面出现 Currently out of stock  -> 无货
    页面出现 Available immediately   -> 有货

环境变量：
    KELLER_URL        必填，商品页面地址；多个地址用换行、逗号或分号分隔
    KELLER_MODE       0：有货才通知（默认，无货不通知）
                      1：每次巡检都通知（无论有无货）
                      2：仅当商品由无货转为有货时通知
    KELLER_INTERVAL   检查间隔，单位分钟，默认 10
    KELLER_ONCE       设为 1 时只检查一次就退出，适合配合青龙 cron 定时运行
    KELLER_STATE_FILE 状态文件路径，可选；默认优先存放在 /ql/data 下

通知相关（脚本直推专用变量，统一加 _KELLER 后缀，避免与青龙内置渠道重复推送）：
    Bark:
      BARK_PUSH_KELLER / BARK_SERVER_KELLER
      BARK_GROUP_KELLER / BARK_SOUND_KELLER / BARK_ICON_KELLER / BARK_LEVEL_KELLER / BARK_URL_KELLER
    ntfy:
      NTFY_URL_KELLER / NTFY_TOPIC_KELLER / NTFY_PRIORITY_KELLER
      NTFY_TOKEN_KELLER / NTFY_USERNAME_KELLER / NTFY_PASSWORD_KELLER
    邮件（SMTP）:
      SMTP_SERVER_KELLER（host:port）/ SMTP_SSL_KELLER（true 或 false）
      SMTP_EMAIL_KELLER / SMTP_PASSWORD_KELLER
      SMTP_EMAIL_TO_KELLER（可选，多个用分号）/ SMTP_NAME_KELLER（可选）
    Telegram:
      TG_BOT_TOKEN_KELLER / TG_USER_ID_KELLER / TG_API_HOST_KELLER
      TG_PROXY_HOST_KELLER / TG_PROXY_PORT_KELLER / TG_PROXY_AUTH_KELLER（可选）
    企业微信:
      QYWX_KEY_KELLER（群机器人）
      或 QYWX_AM_KELLER（应用消息：corpid,corpsecret,touser,agentid[,media_id]）
      QYWX_ORIGIN_KELLER（可选，代理地址）
    Server酱:
      PUSH_KEY_KELLER / SERVERCHAN_URL_KELLER（可选，自定义服务地址）

    推送策略（互斥，保证同一条通知同一渠道只发一次）：
    - 配置了任一 *_KELLER 变量：只由脚本直推对应渠道，不调用青龙 notify.py；
    - 未配置 *_KELLER 变量：回退青龙 notify.py，脚本不再用标准变量直推。
    - 企业微信同时配置 QYWX_KEY_KELLER 和 QYWX_AM_KELLER 时，只发群机器人，避免重复。

说明：
    - 脚本默认常驻循环，每隔 KELLER_INTERVAL 分钟检查一次。
    - 青龙里也可以把定时规则设为 */10 * * * *，并设置 KELLER_ONCE=1 单次执行，
      这样容器重启后由 cron 继续拉起，更稳妥。
    - MODE=2 首次运行时只记录基线状态（无货静默记录；有货也仅记录，避免部署瞬间误报），
      之后只有“无货 -> 有货”的转换才会通知。状态保存在持久化文件中，重启不丢失。
"""

import base64
import contextlib
import html
import io
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

try:
    from notify import send
    HAD_SEND = True
except ImportError:
    HAD_SEND = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

OUT_OF_STOCK_PATTERNS = [
    r"currently\s+out\s+of\s+stock",
    r"derzeit\s+nicht\s+auf\s+lager",
    r"derzeit\s+nicht\s+verf(?:ü|ue)gbar",
]

IN_STOCK_PATTERNS = [
    r"available\s+immediately",
    r"sofort\s+verf(?:ü|ue)gbar",
]

DEFAULT_INTERVAL_MINUTES = 10
REQUEST_TIMEOUT = 30


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def parse_urls(raw):
    urls = [u.strip() for u in re.split(r"[\s,;]+", raw or "") if u.strip()]
    result = []
    for url in urls:
        if url not in result:
            result.append(url)
    return result


def get_state_file():
    env_path = os.getenv("KELLER_STATE_FILE", "").strip()
    if env_path:
        return env_path
    if os.path.isdir("/ql/data/config"):
        return "/ql/data/config/keller_stock_state.json"
    if os.path.isdir("/ql/data"):
        return "/ql/data/keller_stock_state.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".keller_stock_state.json")


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"⚠️ 读取状态文件失败，按空状态处理: {e}")
        return {}


def save_state(path, state):
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"⚠️ 保存状态文件失败: {e}")


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def extract_title(page):
    match = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    if match:
        title = clean_text(match.group(1))
        if title:
            return title
    match = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    if match:
        title = clean_text(match.group(1))
        title = re.split(r"\s+[-|]\s+keller", title, maxsplit=1)[0].strip()
        return title
    return ""


def extract_main_region(page):
    match = re.search(r"<h1[^>]*>.*?</h1>", page, re.S | re.I)
    start = match.end() if match else 0
    tail = page[start:]
    end = len(tail)
    for marker in (
        'id="tab-description"',
        "id='tab-description'",
        'id="tab-votes"',
        "id='tab-votes'",
        'id="tab-availabilityNotification"',
        "id='tab-availabilityNotification'",
        "Similar items",
    ):
        index = tail.find(marker)
        if index != -1:
            end = min(end, index)
    return tail[:end]


def detect_stock(page):
    region = extract_main_region(page)
    hits = []
    for pattern in OUT_OF_STOCK_PATTERNS:
        match = re.search(pattern, region, re.I)
        if match:
            hits.append((match.start(), False))
    for pattern in IN_STOCK_PATTERNS:
        match = re.search(pattern, region, re.I)
        if match:
            hits.append((match.start(), True))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


def check_url(url):
    result = {
        "url": url,
        "title": "",
        "in_stock": None,
        "error": "",
    }
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        page = response.text
        result["title"] = extract_title(page) or url
        result["in_stock"] = detect_stock(page)
        if result["in_stock"] is None:
            result["error"] = "页面未识别到库存状态，可能页面结构变化或需要登录"
        return result
    except Exception as e:
        result["title"] = url
        result["error"] = str(e)
        return result


def status_text(in_stock):
    if in_stock is True:
        return "✅ 有货（Available immediately）"
    if in_stock is False:
        return "❌ 无货（Currently out of stock）"
    return "⚠️ 状态未知"


def build_message(results):
    blocks = []
    for item in results:
        lines = [
            f"【{item['title'] or item['url']}】",
            f"状态：{status_text(item['in_stock'])}",
        ]
        if item["error"]:
            lines.append(f"备注：{item['error']}")
        lines.append(f"链接：{item['url']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def bark_push(title, content):
    """Bark 直推，专用变量统一使用 _KELLER 后缀"""
    push = os.getenv("BARK_PUSH_KELLER", "").strip()
    if not push:
        return False, "未配置 BARK_PUSH_KELLER"

    server = (os.getenv("BARK_SERVER_KELLER", "") or "https://api.day.app").strip().rstrip("/")
    if push.startswith(("http://", "https://")):
        match = re.match(r"^(https?://[^/]+)(/.*)?$", push.rstrip("/"))
        if not match:
            return False, f"BARK_PUSH_KELLER 地址格式不正确: {push}"
        server = match.group(1)
        key = (match.group(2) or "").strip("/")
        if not key:
            return False, f"BARK_PUSH_KELLER 地址中缺少设备码: {push}"
    else:
        key = push

    payload = {"device_key": key, "title": title, "body": content}
    for env_name, field in (
        ("BARK_GROUP_KELLER", "group"),
        ("BARK_SOUND_KELLER", "sound"),
        ("BARK_ICON_KELLER", "icon"),
        ("BARK_LEVEL_KELLER", "level"),
        ("BARK_URL_KELLER", "url"),
    ):
        value = os.getenv(env_name, "").strip()
        if value:
            payload[field] = value

    try:
        response = requests.post(f"{server}/push", json=payload, timeout=15)
        response.raise_for_status()
        return True, response.text.strip()[:200]
    except Exception as e:
        return False, str(e)


def ntfy_push(title, content):
    """ntfy 直推，专用变量统一使用 _KELLER 后缀"""
    server = (os.getenv("NTFY_URL_KELLER", "") or "").strip().rstrip("/")
    topic = os.getenv("NTFY_TOPIC_KELLER", "").strip()
    if not server:
        return False, "未配置 NTFY_URL_KELLER，例如 https://ntfy.sh"
    if not topic:
        return False, "未配置 NTFY_TOPIC_KELLER"

    priority = os.getenv("NTFY_PRIORITY_KELLER", "3").strip() or "3"
    payload = {"topic": topic, "title": title, "message": content}
    payload["priority"] = int(priority) if priority.isdigit() else priority

    headers = {}
    token = os.getenv("NTFY_TOKEN_KELLER", "").strip()
    username = os.getenv("NTFY_USERNAME_KELLER", "").strip()
    password = os.getenv("NTFY_PASSWORD_KELLER", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif username and password:
        credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {credentials}"

    try:
        response = requests.post(server, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return True, response.text.strip()[:200]
    except Exception as e:
        return False, str(e)


def smtp_push(title, content):
    """SMTP 邮件直推，变量统一使用 _KELLER 后缀"""
    server_raw = os.getenv("SMTP_SERVER_KELLER", "").strip()
    sender = os.getenv("SMTP_EMAIL_KELLER", "").strip()
    password = os.getenv("SMTP_PASSWORD_KELLER", "").strip()
    use_ssl = os.getenv("SMTP_SSL_KELLER", "false").strip().lower() in ("1", "true", "yes")
    name = os.getenv("SMTP_NAME_KELLER", "").strip() or "Keller Monitor"
    receiver_raw = os.getenv("SMTP_EMAIL_TO_KELLER", "").strip() or sender

    if not server_raw:
        return False, "未配置 SMTP_SERVER_KELLER"
    if not sender or not password:
        return False, "未配置 SMTP_EMAIL_KELLER 或 SMTP_PASSWORD_KELLER"
    receivers = [item.strip() for item in re.split(r"[;；,，]", receiver_raw) if item.strip()]
    if not receivers:
        return False, "未配置有效收件人 SMTP_EMAIL_TO_KELLER"

    if ":" in server_raw:
        host, port_text = server_raw.rsplit(":", 1)
        port = int(port_text) if port_text.isdigit() else (465 if use_ssl else 25)
    else:
        host = server_raw
        port = 465 if use_ssl else 25

    message = MIMEText(f"{title}\n\n{content}", "plain", "utf-8")
    message["From"] = formataddr((str(Header(name, "utf-8")), sender))
    message["To"] = ",".join(receivers)
    message["Subject"] = Header(title, "utf-8")

    client = None
    try:
        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            client = smtplib.SMTP(host, port, timeout=20)
            client.ehlo()
            if client.has_extn("starttls"):
                client.starttls()
                client.ehlo()
        client.login(sender, password)
        client.sendmail(sender, receivers, message.as_bytes())
        return True, f"{len(receivers)} 个收件人"
    except Exception as e:
        return False, str(e)
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass


def telegram_push(title, content):
    """Telegram 机器人直推，变量统一使用 _KELLER 后缀"""
    token = os.getenv("TG_BOT_TOKEN_KELLER", "").strip()
    chat_id = os.getenv("TG_USER_ID_KELLER", "").strip()
    api_host = (os.getenv("TG_API_HOST_KELLER", "") or "https://api.telegram.org").strip().rstrip("/")
    if not token:
        return False, "未配置 TG_BOT_TOKEN_KELLER"
    if not chat_id:
        return False, "未配置 TG_USER_ID_KELLER"

    proxies = None
    proxy_host = os.getenv("TG_PROXY_HOST_KELLER", "").strip()
    proxy_port = os.getenv("TG_PROXY_PORT_KELLER", "").strip()
    proxy_auth = os.getenv("TG_PROXY_AUTH_KELLER", "").strip()
    if proxy_host and proxy_port:
        if proxy_auth and "@" not in proxy_host:
            proxy_host = f"{proxy_auth}@{proxy_host}"
        proxy_url = f"http://{proxy_host}:{proxy_port}"
        proxies = {"http": proxy_url, "https": proxy_url}

    payload = {
        "chat_id": chat_id,
        "text": f"{title}\n\n{content}",
        "disable_web_page_preview": "true",
    }
    try:
        response = requests.post(
            f"{api_host}/bot{token}/sendMessage",
            params=payload,
            proxies=proxies,
            timeout=20,
        )
        result = response.json()
        if result.get("ok"):
            return True, "ok"
        return False, result.get("description") or str(result)
    except Exception as e:
        return False, str(e)


def wecom_push(title, content):
    """企业微信直推：优先群机器人，未配置时走应用消息，避免重复发送"""
    key = os.getenv("QYWX_KEY_KELLER", "").strip()
    app_config = os.getenv("QYWX_AM_KELLER", "").strip()
    origin = (os.getenv("QYWX_ORIGIN_KELLER", "") or "https://qyapi.weixin.qq.com").strip().rstrip("/")
    if not key and not app_config:
        return False, "未配置 QYWX_KEY_KELLER 或 QYWX_AM_KELLER"

    text = f"{title}\n\n{content}"
    try:
        if key:
            response = requests.post(
                f"{origin}/cgi-bin/webhook/send",
                params={"key": key},
                json={"msgtype": "text", "text": {"content": text}},
                timeout=20,
            )
            result = response.json()
            if result.get("errcode") == 0:
                return True, "群机器人"
            return False, result.get("errmsg") or str(result)

        parts = [part.strip() for part in app_config.split(",")]
        if len(parts) < 4:
            return False, "QYWX_AM_KELLER 格式应为 corpid,corpsecret,touser,agentid[,media_id]"
        corpid, corpsecret, touser, agentid = parts[:4]
        media_id = parts[4] if len(parts) >= 5 else ""
        touser = touser or "@all"

        token_resp = requests.post(
            f"{origin}/cgi-bin/gettoken",
            params={"corpid": corpid, "corpsecret": corpsecret},
            timeout=20,
        ).json()
        access_token = token_resp.get("access_token")
        if not access_token:
            return False, token_resp.get("errmsg") or str(token_resp)

        agent_value = int(agentid) if agentid.isdigit() else agentid
        if media_id:
            payload = {
                "touser": touser,
                "msgtype": "mpnews",
                "agentid": agent_value,
                "mpnews": {
                    "articles": [
                        {
                            "title": title,
                            "thumb_media_id": media_id,
                            "author": "Author",
                            "content_source_url": "",
                            "content": content.replace("\n", "<br/>"),
                            "digest": content,
                        }
                    ]
                },
            }
        else:
            payload = {
                "touser": touser,
                "msgtype": "text",
                "agentid": agent_value,
                "text": {"content": text},
                "safe": "0",
            }

        response = requests.post(
            f"{origin}/cgi-bin/message/send",
            params={"access_token": access_token},
            json=payload,
            timeout=20,
        )
        result = response.json()
        if result.get("errcode") == 0:
            return True, "应用消息"
        return False, result.get("errmsg") or str(result)
    except Exception as e:
        return False, str(e)


def serverchan_push(title, content):
    """Server酱直推，变量统一使用 _KELLER 后缀"""
    key = os.getenv("PUSH_KEY_KELLER", "").strip()
    base = (os.getenv("SERVERCHAN_URL_KELLER", "") or "").strip().rstrip("/")
    if not key:
        return False, "未配置 PUSH_KEY_KELLER"

    if base:
        url = f"{base}/{key}.send"
    else:
        match = re.match(r"sctp(\d+)t", key)
        if match:
            url = f"https://{match.group(1)}.push.ft07.com/send/{key}.send"
        else:
            url = f"https://sctapi.ftqq.com/{key}.send"

    try:
        response = requests.post(
            url,
            data={"text": title, "desp": content.replace("\n", "\n\n")},
            timeout=20,
        )
        result = response.json()
        if result.get("errno") == 0 or result.get("code") == 0:
            return True, "ok"
        return False, result.get("message") or str(result)
    except Exception as e:
        return False, str(e)


def direct_channels():
    channels = []
    if os.getenv("BARK_PUSH_KELLER", "").strip():
        channels.append("Bark")
    if os.getenv("NTFY_TOPIC_KELLER", "").strip():
        channels.append("ntfy")
    if os.getenv("SMTP_SERVER_KELLER", "").strip():
        channels.append("邮件")
    if os.getenv("TG_BOT_TOKEN_KELLER", "").strip() and os.getenv("TG_USER_ID_KELLER", "").strip():
        channels.append("Telegram")
    if os.getenv("QYWX_KEY_KELLER", "").strip() or os.getenv("QYWX_AM_KELLER", "").strip():
        channels.append("企业微信")
    if os.getenv("PUSH_KEY_KELLER", "").strip():
        channels.append("Server酱")
    return channels


def send_direct(title, content, channels):
    delivered = []
    push_funcs = (
        ("Bark", bark_push),
        ("ntfy", ntfy_push),
        ("邮件", smtp_push),
        ("Telegram", telegram_push),
        ("企业微信", wecom_push),
        ("Server酱", serverchan_push),
    )
    for name, push_func in push_funcs:
        if name not in channels:
            continue
        success, detail = push_func(title, content)
        if success:
            delivered.append(name)
            log(f"✅ {name} 直推成功: {detail}")
        else:
            log(f"❌ {name} 直推失败: {detail}")
    return delivered


def notify(title, content):
    channels = direct_channels()
    if channels:
        log(f"通知方式: 脚本直推（{'、'.join(channels)}），不调用 notify.py，避免重复推送")
        delivered = send_direct(title, content, channels)
        if delivered:
            log(f"✅ 通知通过 {'、'.join(delivered)} 发送完成")
            return True
        log("❌ 所有 *_KELLER 直推渠道均发送失败")
        log(f"📢 {title}\n{content}")
        return False

    if not HAD_SEND:
        log("⚠️ 未配置 *_KELLER 渠道且未加载 notify.py，仅打印通知内容")
        log(f"📢 {title}\n{content}")
        return False

    log("通知方式: 回退青龙 notify.py（未配置 *_KELLER 专用渠道）")
    output = ""
    call_error = ""
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            send(title, content)
        output = buffer.getvalue().strip()
    except Exception as e:
        call_error = str(e)
        log(f"❌ notify.py 调用异常: {e}")

    for line in output.splitlines():
        if line.strip():
            log(f"  notify.py: {line.strip()}")

    no_channel = ("无推送渠道" in output) or ("没有推送渠道" in output)
    if call_error or no_channel:
        log("❌ 青龙 notify.py 没有可用渠道；为避免与标准 BARK_*/NTFY_* 重复，脚本不再直推")
        log("如需脚本直推，请配置任一 *_KELLER 渠道变量（BARK / NTFY / SMTP / TG / QYWX / PUSH_KEY）")
        log(f"📢 {title}\n{content}")
        return False

    if any(word in output for word in ("失败", "错误", "error", "Error")):
        log("⚠️ notify.py 日志中出现失败信息，请结合上方输出检查渠道配置")
    log(f"✅ 通知发送完成: {title}")
    return True


def run_once(urls, mode, state, state_file):
    log(f"开始巡检，共 {len(urls)} 个商品，模式 KELLER_MODE={mode}")
    results = []
    transitions = []

    for index, url in enumerate(urls):
        item = check_url(url)
        results.append(item)

        previous = state.get(url, {})
        previous_stock = previous.get("in_stock")
        has_baseline = "in_stock" in previous and previous_stock is not None

        if item["in_stock"] is True and previous_stock is False:
            transitions.append(item)

        if item["in_stock"] is None:
            log(f"[{index + 1}/{len(urls)}] {item['title']} -> 状态未知: {item['error']}")
        else:
            log(
                f"[{index + 1}/{len(urls)}] {item['title']} -> "
                f"{'有货' if item['in_stock'] else '无货'}"
            )

        record = dict(previous)
        record["title"] = item["title"]
        if item["in_stock"] is not None:
            record["in_stock"] = item["in_stock"]
        record["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record["last_error"] = item["error"]
        state[url] = record

        if mode == 2 and not has_baseline:
            log(f"    ↳ 首次建立基线（当前{'有货' if item['in_stock'] else '无货/未知'}），本次不通知")

        if index < len(urls) - 1:
            time.sleep(2)

    save_state(state_file, state)

    in_stock_items = [r for r in results if r["in_stock"] is True]
    notify_title = "Keller 商品库存巡检"
    should_send = False

    if mode == 2:
        if transitions:
            should_send = True
            notify_title = "Keller 商品补货提醒（无货 → 有货）"
            log("检测到补货商品: " + "、".join(t["title"] for t in transitions))
        else:
            log("未检测到“无货 -> 有货”的转换，本次不通知")
    elif mode == 1:
        should_send = True
        notify_title = "Keller 商品库存巡检"
    else:
        if in_stock_items:
            should_send = True
            notify_title = "Keller 商品有货通知"
        else:
            log("当前没有有货商品，本次不通知")

    if should_send:
        notify(notify_title, build_message(results))

    return results


def main():
    raw_urls = os.getenv("KELLER_URL", "")
    urls = parse_urls(raw_urls)
    if not urls:
        message = "❌ 未配置 KELLER_URL 环境变量，请填写要监控的商品页面地址（多个用换行、逗号或分号分隔）"
        log(message)
        notify("Keller 商品库存监控配置错误", message)
        sys.exit(1)

    mode_raw = os.getenv("KELLER_MODE", "0").strip()
    if mode_raw not in ("0", "1", "2"):
        log(f"⚠️ KELLER_MODE={mode_raw} 无效，回退为 0")
        mode_raw = "0"
    mode = int(mode_raw)

    interval_raw = os.getenv("KELLER_INTERVAL", str(DEFAULT_INTERVAL_MINUTES)).strip()
    try:
        interval = float(interval_raw)
    except ValueError:
        log(f"⚠️ KELLER_INTERVAL={interval_raw} 无效，使用默认 {DEFAULT_INTERVAL_MINUTES} 分钟")
        interval = DEFAULT_INTERVAL_MINUTES
    if interval <= 0:
        log(f"⚠️ KELLER_INTERVAL={interval_raw} 必须大于 0，使用默认 {DEFAULT_INTERVAL_MINUTES} 分钟")
        interval = DEFAULT_INTERVAL_MINUTES

    once = os.getenv("KELLER_ONCE", "0").strip().lower() in ("1", "true", "yes")

    state_file = get_state_file()
    state = load_state(state_file)

    log(f"状态文件: {state_file}")
    log(f"通知模块: {'已加载 notify.py' if HAD_SEND else '未加载 notify.py，仅日志输出'}")
    direct = direct_channels()
    if direct:
        log(f"通知渠道: 脚本直推 {'、'.join(direct)}（不调用 notify.py，避免重复推送）")
    elif HAD_SEND:
        log("通知渠道: 未配置 *_KELLER 专用变量，回退青龙 notify.py")
    else:
        log("通知渠道: 未配置 *_KELLER 且未加载 notify.py，仅日志输出")
    log(f"监控商品数: {len(urls)}，间隔: {interval:g} 分钟，模式: {mode}")
    for url in urls:
        log(f"  - {url}")

    while True:
        try:
            run_once(urls, mode, state, state_file)
        except Exception as e:
            log(f"❌ 本轮巡检异常: {e}")

        if once:
            log("KELLER_ONCE=1，单次巡检结束")
            break

        next_time = datetime.now() + timedelta(minutes=interval)
        log(f"本轮结束，下次检查时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            log("收到中断信号，脚本退出")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("收到中断信号，脚本退出")