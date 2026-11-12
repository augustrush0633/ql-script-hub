#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cron: 7 8,12,16,20 * * *
# new Env('洛克王国远行商人商品推送')
"""洛克王国远行商人商品爬虫 - 青龙脚本

抓取当前时间段商品并推送至企业微信 / 飞书 / Bark / ntfy。
所有配置通过环境变量注入，未配置的渠道会自动跳过。
"""

import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
except ImportError as exc:
    print(f"❌ 缺少依赖: {exc}")
    print("请在青龙面板『依赖管理 → Python3』中安装: requests beautifulsoup4")
    sys.exit(1)

URL = "https://www.onebiji.com/hykb_tools/comm/lkwgmerchant/preview.php?id=1&immgj=0"
TITLE = "🏪 洛克王国远行商人商品提醒"
DEFAULT_GROUP = "洛克王国"
MAX_MESSAGE_BYTES = 3800

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.onebiji.com/",
}


class PushError(Exception):
    """推送接口返回业务错误（不重试）"""


def _make_session():
    """创建带自动重试的 requests Session"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


session = _make_session()


def _env(name, default=""):
    """读取环境变量并清理首尾空白与引号"""
    value = os.environ.get(name, default)
    if value is None:
        return default
    return value.strip().strip('"').strip("'")


def load_config():
    """从青龙环境变量加载全部配置"""
    return {
        "wecom_url": _env("WECOM_WEBHOOK_URL"),
        "feishu_url": _env("FEISHU_WEBHOOK_URL"),
        "bark_push": _env("BARK_PUSH"),
        "bark_key": _env("BARK_KEY"),
        "bark_server": _env("BARK_SERVER") or "https://api.day.app",
        "bark_group": _env("BARK_GROUP") or DEFAULT_GROUP,
        "bark_sound": _env("BARK_SOUND"),
        "bark_icon": _env("BARK_ICON"),
        "bark_level": _env("BARK_LEVEL"),
        "ntfy_url": _env("NTFY_URL"),
        "ntfy_server": _env("NTFY_SERVER") or "https://ntfy.sh",
        "ntfy_topic": _env("NTFY_TOPIC"),
        "ntfy_token": _env("NTFY_TOKEN"),
        "ntfy_priority": _env("NTFY_PRIORITY"),
        "ntfy_tags": _env("NTFY_TAGS"),
        "rock_value": _env("ROCK_VALUE") or "3",
    }

#ROCK_VALUE 0 不显示，1 仅强烈推荐，2 仅超高价值，3 全部显示

def resolve_bark(cfg):
    """解析 Bark 服务地址与设备 Key，返回 (server, key) 或 None"""
    server = (cfg["bark_server"] or "https://api.day.app").rstrip("/")
    key = cfg["bark_key"]
    push = cfg["bark_push"]

    if push:
        if push.startswith(("http://", "https://")):
            match = re.match(r"^(https?://[^/]+)(/.*)?$", push)
            if not match:
                return None
            path = (match.group(2) or "").strip("/")
            parts = path.split("/") if path else []
            prefix = "/".join(parts[:-1]) if len(parts) > 1 else ""
            server = match.group(1) + (f"/{prefix}" if prefix else "")
            if not key and parts and parts[-1] != "push":
                key = parts[-1]
        elif not key:
            key = push.strip("/")

    if not key:
        return None
    return server.rstrip("/"), key


def resolve_ntfy(cfg):
    """解析 ntfy 地址与主题，返回 (server, topic, publish_url) 或 None"""
    server = (cfg["ntfy_server"] or "https://ntfy.sh").rstrip("/")
    url = cfg["ntfy_url"]
    topic = cfg["ntfy_topic"]

    if url:
        if url.startswith(("http://", "https://")):
            match = re.match(r"^(https?://[^/]+)(/.*)?$", url)
            if not match:
                return None
            path = (match.group(2) or "").strip("/")
            if path:
                parts = path.split("/")
                topic = parts[-1]
                prefix = "/".join(parts[:-1])
                server = match.group(1) + (f"/{prefix}" if prefix else "")
            elif not topic:
                return None
        else:
            topic = topic or url.strip("/")
    elif not topic:
        return None

    return server.rstrip("/"), topic, f"{server.rstrip('/')}/{topic}"


def resolve_rock_value(cfg):
    """解析 ROCK_VALUE：0 不显示，1 仅强烈推荐，2 仅超高价值，3 全部显示"""
    value = (cfg.get("rock_value") or "3").strip()
    if value not in ("0", "1", "2", "3"):
        print(f"⚠️ 未知的 ROCK_VALUE={value}，使用默认值 3（全部显示）")
        return 3
    return int(value)


def product_marks(product, rock_value=3):
    """按 ROCK_VALUE 返回商品需要展示的角标"""
    marks = []
    if rock_value in (1, 3) and product.get("recommend"):
        marks.append("🔥强烈推荐")
    if rock_value in (2, 3) and product.get("high_value"):
        marks.append("💎超高价值")
    return marks


def configured_channels(cfg):
    """返回已配置完整的渠道名称列表"""
    channels = []
    if cfg["wecom_url"]:
        channels.append("企业微信")
    if cfg["feishu_url"]:
        channels.append("飞书")
    if resolve_bark(cfg):
        channels.append("Bark")
    elif cfg["bark_push"] or cfg["bark_key"]:
        print("⚠️ Bark 配置不完整（无法解析设备 Key），已跳过")
    if resolve_ntfy(cfg):
        channels.append("ntfy")
    elif cfg["ntfy_url"] or cfg["ntfy_topic"]:
        print("⚠️ ntfy 配置不完整（缺少主题），已跳过")
    return channels


def fetch_page():
    """获取页面 HTML"""
    resp = session.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def parse_products(html):
    """解析页面中的商品列表，返回 (products, time_period, no_merchant)"""
    soup = BeautifulSoup(html, "html.parser")
    products = []

    time_period = ""
    active_time = soup.select_one(".time-list li[class*='on']")
    if active_time:
        ems = active_time.find_all("em")
        if len(ems) >= 2:
            time_period = f"{ems[0].get_text(strip=True)} - {ems[1].get_text(strip=True)}"

    tip_div = soup.select_one("#shop_tip")
    if tip_div and "display:none" not in tip_div.get("style", "").replace(" ", ""):
        return [], time_period, True

    server_now = 0
    sn_match = re.search(r"var\s+serverNow\s*=\s*(\d+)", html)
    if sn_match:
        server_now = int(sn_match.group(1))

    items = soup.select(".shop-list li.all_show.li_show")
    for item in items:
        product = {}

        name_tag = item.select_one(".shop_name")
        product["name"] = name_tag.get_text(strip=True) if name_tag else "未知"

        price_tag = item.select_one(".shop_price")
        if price_tag:
            price_text = price_tag.get_text(strip=True)
            match = re.search(r"：([\d.w]+)", price_text)
            product["price"] = match.group(1).strip() if match else "未知"
        else:
            product["price"] = "未知"

        limit_tag = item.select_one(".gitem em")
        if limit_tag:
            limit_text = limit_tag.get_text(strip=True)
            match = re.search(r"(\d+)", limit_text)
            product["limit"] = match.group(1) if match else "无限制"
        else:
            product["limit"] = "无限制"

        img_tag = item.select_one(".gitem img")
        product["image"] = img_tag["src"] if img_tag else ""

        onclick = item.get("onclick", "")
        desc_match = re.search(r"showShopinfo\([^,]+,[^,]+,[^,]+,'([^']*)'\)", onclick)
        product["desc"] = desc_match.group(1) if desc_match else ""

        type_match = re.search(r"showShopinfo\([^,]+,[^,]+,'([^']*)'", onclick)
        product["type"] = type_match.group(1) if type_match else ""

        product["high_value"] = bool(item.select_one("div.tp1"))
        product["recommend"] = bool(item.select_one("div.tp2"))

        end_time = int(item.get("data-time", 0))
        if server_now and end_time:
            surplus = end_time - server_now
            if surplus > 0:
                h = surplus // 3600
                m = (surplus % 3600) // 60
                product["remain"] = f"{h}小时{m}分钟"
            else:
                product["remain"] = "已结束"
        else:
            product["remain"] = "未知"

        if product.get("remain") == "已结束":
            continue

        products.append(product)

    return products, time_period, False


def truncate_utf8(text, max_bytes=MAX_MESSAGE_BYTES):
    """按 UTF-8 字节数安全截断，避免截坏中文字符"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text

    suffix = "\n…（内容过长已截断）"
    limit = max(max_bytes - len(suffix.encode("utf-8")), 0)
    data = data[:limit]
    while data:
        try:
            return data.decode("utf-8") + suffix
        except UnicodeDecodeError:
            data = data[:-1]
    return suffix


def build_message(products, time_period, rock_value=3):
    """构造企业微信 Markdown 消息"""
    lines = [
        "# 🏪 洛克王国远行商人商品提醒",
        f"> **当前时间段**: {time_period}",
        "",
    ]

    for i, p in enumerate(products, 1):
        name = p['name']
        tags = product_marks(p, rock_value)
        if tags:
            name = f"{name} <font color=\"warning\">{' '.join(tags)}</font>"
        lines.append(f"**{i}. {name}**")
        lines.append(f"> 价格: <font color=\"warning\">{p['price']}</font> 洛克贝")
        lines.append(f"> 限购: {p['limit']}")
        if p.get("remain"):
            lines.append(f"> ⏳ 剩余时间: <font color=\"info\">{p['remain']}</font>")
        if p.get("type"):
            lines.append(f"> 类型: {p['type']}")
        if p.get("desc"):
            lines.append(f"> {p['desc']}")
        lines.append("")

    lines.append(f"[📎 点击查看详情]({URL})")
    return "\n".join(lines)


def build_plain_message(products, time_period, rock_value=3):
    """构造 Bark / ntfy 使用的精简纯文本消息"""
    lines = [
        f"⏰ 当前时间段: {time_period}",
    ]

    for i, p in enumerate(products, 1):
        segments = [f"{i}. {p['name']}", *product_marks(p, rock_value)]
        segments.append(f"价格: {p['price']} 洛克贝")
        segments.append(f"限购: {p['limit']}")
        lines.append(" | ".join(segments))

    lines.append("")
    lines.append(f"🔗 查看详情: {URL}")
    return "\n".join(lines)


def build_no_merchant_message():
    """无商人时段的企业微信消息"""
    return (
        "# 🏪 洛克王国远行商人商品提醒\n"
        "> **当前时段**: 00:00 - 08:00\n"
        "> ⏰ 每日0:00-8:00无远行商人，请等待下个时间段\n"
        f"\n[📎 点击查看详情]({URL})"
    )


def build_no_merchant_plain():
    """无商人时段的纯文本消息"""
    return (
        "⏰ 当前时段: 00:00 - 08:00\n\n"
        "每日 0:00-8:00 无远行商人，请等待下个时间段。\n\n"
        f"🔗 查看详情: {URL}"
    )


def build_feishu_card(products, time_period, rock_value=3):
    """从商品数据直接构建飞书卡片 payload"""
    md_parts = [f"**⏰ 当前时间段**: {time_period}\n"]

    for i, p in enumerate(products, 1):
        marks = product_marks(p, rock_value)
        tags = []
        if "💎超高价值" in marks:
            tags.append("<font color='orange'>💎超高价值</font>")
        if "🔥强烈推荐" in marks:
            tags.append("<font color='red'>🔥强烈推荐</font>")
        tag_str = " " + " ".join(tags) if tags else ""

        md_parts.append(f"**{i}. {p['name']}**{tag_str}")
        md_parts.append(f"💰 价格: **{p['price']}** 洛克贝 | 🎯 限购: {p['limit']}")
        if p.get("remain"):
            md_parts.append(f"⏳ 剩余: **{p['remain']}**")
        if p.get("type"):
            md_parts.append(f"📂 {p['type']}")
        if p.get("desc"):
            md_parts.append(f"📝 {p['desc']}")
        md_parts.append("")

    md_content = truncate_utf8("\n".join(md_parts), 20000)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": TITLE},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": md_content},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "📎 查看详情"},
                            "type": "primary",
                            "url": URL,
                        }
                    ],
                },
            ],
        },
    }


def build_feishu_no_merchant_card():
    """无商人时段的飞书卡片"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": TITLE},
                "template": "grey",
            },
            "elements": [
                {"tag": "markdown", "content": "**⏰ 当前时段**: 00:00 - 08:00\n\n当前无远行商人，请等待下个时间段。"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "📎 查看详情"},
                            "type": "primary",
                            "url": URL,
                        }
                    ],
                },
            ],
        },
    }


def build_feishu_error_card(error):
    """抓取失败的飞书卡片"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "⚠️ 洛克王国远行商人"}, "template": "red"},
            "elements": [{"tag": "markdown", "content": f"**抓取失败**\n\n错误信息: {error}"}],
        },
    }


def _retry(fn, retries=3, backoff=1):
    """带指数退避的重试，业务错误与 4xx 不重试"""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except PushError:
            raise
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status and status < 500:
                raise
            if attempt == retries:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(f"⚠️  第{attempt}次失败，{wait}秒后重试: {exc}")
            time.sleep(wait)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(f"⚠️  第{attempt}次失败，{wait}秒后重试: {exc}")
            time.sleep(wait)


def send_to_wecom(cfg, content):
    """推送消息到企业微信机器人"""
    webhook = cfg["wecom_url"]
    if not webhook:
        print("⏭️  未设置 WECOM_WEBHOOK_URL，跳过企业微信推送")
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": truncate_utf8(content),
        },
    }

    def _do():
        resp = session.post(webhook, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            raise PushError(f"企业微信返回错误: {result}")
        return result

    try:
        _retry(_do)
        print("✅ 企业微信消息推送成功")
        return True
    except Exception as exc:
        print(f"❌ 企业微信推送失败: {exc}")
        return False


def send_to_feishu(cfg, payload):
    """推送飞书卡片消息"""
    webhook = cfg["feishu_url"]
    if not webhook:
        print("⏭️  未设置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return False

    def _do():
        resp = session.post(webhook, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0 and result.get("StatusCode") != 0:
            raise PushError(f"飞书返回错误: {result}")
        return result

    try:
        _retry(_do)
        print("✅ 飞书消息推送成功")
        return True
    except Exception as exc:
        print(f"❌ 飞书推送失败: {exc}")
        return False


def send_to_bark(cfg, title, body):
    """推送消息到 Bark"""
    target = resolve_bark(cfg)
    if not target:
        print("⏭️  未配置 Bark，跳过 Bark 推送")
        return False

    server, key = target
    payload = {
        "device_key": key,
        "title": title,
        "body": truncate_utf8(body),
        "group": cfg["bark_group"],
    }
    for opt in ("sound", "icon", "level"):
        value = cfg[f"bark_{opt}"]
        if value:
            payload[opt] = value

    def _do():
        resp = session.post(f"{server}/push", json=payload, timeout=30)
        if resp.status_code in (404, 405):
            fallback = {k: v for k, v in payload.items() if k != "device_key"}
            resp = session.post(f"{server}/{key}", json=fallback, timeout=30)
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError:
            return {}
        if result.get("code") not in (None, 200, "200"):
            raise PushError(f"Bark 返回错误: {result}")
        return result

    try:
        _retry(_do)
        print("✅ Bark 消息推送成功")
        return True
    except Exception as exc:
        print(f"❌ Bark 推送失败: {exc}")
        return False


def send_to_ntfy(cfg, title, body, priority="default", tags=None):
    """推送消息到 ntfy"""
    target = resolve_ntfy(cfg)
    if not target:
        print("⏭️  未配置 ntfy，跳过 ntfy 推送")
        return False

    server, topic, publish_url = target
    payload = {
        "title": title,
        "message": truncate_utf8(body),
        "priority": priority,
        "tags": tags or ["bell"],
        "click": URL,
    }
    headers = {"Markdown": "yes"}
    if cfg["ntfy_token"]:
        headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"

    def _do():
        resp = session.post(publish_url, json=payload, headers=headers, timeout=30)
        if resp.status_code in (404, 405):
            fallback = dict(payload)
            fallback["topic"] = topic
            resp = session.post(server, json=fallback, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp

    try:
        _retry(_do)
        print("✅ ntfy 消息推送成功")
        return True
    except Exception as exc:
        print(f"❌ ntfy 推送失败: {exc}")
        return False


def resolve_ntfy_priority(cfg, auto_priority):
    """解析 ntfy 优先级配置，支持数字与 min/low/default/high/urgent"""
    value = (cfg["ntfy_priority"] or "").strip().lower()
    if not value:
        return auto_priority
    if value.isdigit():
        return int(value)
    if value in ("min", "low", "default", "high", "urgent"):
        return value
    print(f"⚠️ 未知的 NTFY_PRIORITY={value}，使用默认值 {auto_priority}")
    return auto_priority


def resolve_ntfy_tags(cfg, auto_tags):
    """解析 ntfy 标签配置"""
    value = cfg["ntfy_tags"]
    if not value:
        return auto_tags
    tags = [tag.strip() for tag in re.split(r"[,，\s]+", value) if tag.strip()]
    return tags or auto_tags


def main():
    cfg = load_config()
    rock_value = resolve_rock_value(cfg)
    channels = configured_channels(cfg)
    if not channels:
        print("❌ 未配置任何推送渠道，请至少配置以下任意一组环境变量：")
        print("   企业微信: WECOM_WEBHOOK_URL")
        print("   飞书:     FEISHU_WEBHOOK_URL")
        print("   Bark:     BARK_PUSH 或 BARK_KEY")
        print("   ntfy:     NTFY_URL 或 NTFY_TOPIC / NTFY_SERVER")
        return 1

    print(f"🕒 运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📢 已配置渠道: {'、'.join(channels)}")
    print(f"🏷️ 商品角标: ROCK_VALUE={rock_value}")
    print("🔍 正在抓取远行商人商品信息...")

    fetch_error = None
    try:
        html = fetch_page()
    except Exception as exc:
        fetch_error = exc
        wecom_msg = (
            "# ⚠️ 洛克王国远行商人\n"
            "> **抓取失败，请检查网站是否可用**\n\n"
            f"**错误信息**: {exc}\n"
        )
        feishu_payload = build_feishu_error_card(exc)
        plain_title = "⚠️ 洛克王国远行商人抓取失败"
        plain_body = f"抓取失败，请检查网站是否可用。\n\n错误信息: {exc}\n\n🔗 查看详情: {URL}"
        auto_priority = "high"
        auto_tags = ["warning"]
    else:
        products, time_period, is_no_merchant = parse_products(html)

        if is_no_merchant:
            print("⏰ 当前为无商人时段 (0:00-8:00)")
            wecom_msg = build_no_merchant_message()
            feishu_payload = build_feishu_no_merchant_card()
            plain_title = TITLE
            plain_body = build_no_merchant_plain()
            auto_priority = "low"
            auto_tags = ["zzz"]
        elif not products:
            print("⚠️ 未找到商品信息")
            wecom_msg = (
                "# 🏪 洛克王国远行商人商品提醒\n"
                "> ⚠️ 未能获取到商品信息，请手动查看\n"
                f"\n[📎 点击查看详情]({URL})"
            )
            feishu_payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"tag": "plain_text", "content": TITLE}, "template": "orange"},
                    "elements": [
                        {"tag": "markdown", "content": "**⚠️ 未能获取到商品信息**，请手动查看"},
                        {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📎 查看详情"}, "type": "primary", "url": URL}]},
                    ],
                },
            }
            plain_title = TITLE
            plain_body = f"⚠️ 未能获取到商品信息，请手动查看。\n\n🔗 查看详情: {URL}"
            auto_priority = "default"
            auto_tags = ["warning"]
        else:
            print(f"📦 当前时间段: {time_period}")
            print(f"📦 找到 {len(products)} 个商品:")
            for p in products:
                marks = product_marks(p, rock_value)
                labels = [mark.replace("🔥", "").replace("💎", "") for mark in marks]
                flag = f" [{'、'.join(labels)}]" if labels else ""
                print(f"   - {p['name']}{flag} | 价格: {p['price']} | 限购: {p['limit']}")
            wecom_msg = build_message(products, time_period, rock_value)
            feishu_payload = build_feishu_card(products, time_period, rock_value)
            plain_title = TITLE
            plain_body = build_plain_message(products, time_period, rock_value)
            auto_priority = "high" if any(p.get("recommend") or p.get("high_value") for p in products) else "default"
            auto_tags = ["shopping_cart"]

    priority = resolve_ntfy_priority(cfg, auto_priority)
    tags = resolve_ntfy_tags(cfg, auto_tags)

    print("\n📨 推送消息预览:")
    print("-" * 40)
    print(wecom_msg)
    print("-" * 40)

    results = {
        "企业微信": send_to_wecom(cfg, wecom_msg),
        "飞书": send_to_feishu(cfg, feishu_payload),
        "Bark": send_to_bark(cfg, plain_title, plain_body),
        "ntfy": send_to_ntfy(cfg, plain_title, plain_body, priority, tags),
    }

    succeeded = [name for name in channels if results.get(name)]
    failed = [name for name in channels if not results.get(name)]

    print(f"\n📊 推送结果: 成功 {len(succeeded)}/{len(channels)}")
    if failed:
        print(f"❌ 失败渠道: {'、'.join(failed)}")

    if fetch_error:
        print("⚠️ 本次抓取失败，任务以失败状态结束")
        return 1
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())