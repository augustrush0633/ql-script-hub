#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
洛克王国远行商人商品爬虫 - 抓取当前时间段商品并推送至企业微信/飞书/Bark/ntfy

cron: 7 8,12,16,20 * * *
new Env('洛克王国远行商人商品推送')
"""

import os
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://www.onebiji.com/hykb_tools/comm/lkwgmerchant/preview.php?id=1&immgj=0"
TITLE = "🏪 洛克王国远行商人商品提醒"
WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "")
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.onebiji.com/",
}


def _env(name, default=""):
    """读取环境变量并去除首尾空白"""
    return os.environ.get(name, "").strip() or default


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

    # 获取当前时间段
    time_period = ""
    active_time = soup.select_one(".time-list li[class*='on']")
    if active_time:
        ems = active_time.find_all("em")
        if len(ems) >= 2:
            time_period = f"{ems[0].get_text(strip=True)} - {ems[1].get_text(strip=True)}"

    # 检查是否在无商人时段 (0:00-8:00)：通过提示框是否可见判断
    tip_div = soup.select_one("#shop_tip")
    if tip_div and "display:none" not in tip_div.get("style", "").replace(" ", ""):
        return [], time_period, True

    # 从页面 JS 中提取服务器时间戳 serverNow
    server_now = 0
    sn_match = re.search(r"var\s+serverNow\s*=\s*(\d+)", html)
    if sn_match:
        server_now = int(sn_match.group(1))

    # 解析商品列表 (仅选择可见的商品项，排除占位提示)
    items = soup.select(".shop-list li.all_show.li_show")
    for item in items:
        product = {}

        # 商品名称
        name_tag = item.select_one(".shop_name")
        product["name"] = name_tag.get_text(strip=True) if name_tag else "未知"

        # 商品价格
        price_tag = item.select_one(".shop_price")
        if price_tag:
            price_text = price_tag.get_text(strip=True)
            match = re.search(r"：([\d.w]+)", price_text)
            product["price"] = match.group(1).strip() if match else "未知"
        else:
            product["price"] = "未知"

        # 限购数量
        limit_tag = item.select_one(".gitem em")
        if limit_tag:
            limit_text = limit_tag.get_text(strip=True)
            match = re.search(r"(\d+)", limit_text)
            product["limit"] = match.group(1) if match else "无限制"
        else:
            product["limit"] = "无限制"

        # 商品图片
        img_tag = item.select_one(".gitem img")
        product["image"] = img_tag["src"] if img_tag else ""

        # 从 onclick 提取描述
        onclick = item.get("onclick", "")
        desc_match = re.search(r"showShopinfo\([^,]+,[^,]+,[^,]+,'([^']*)'\)", onclick)
        product["desc"] = desc_match.group(1) if desc_match else ""

        # 商品类型
        type_match = re.search(r"showShopinfo\([^,]+,[^,]+,'([^']*)'", onclick)
        product["type"] = type_match.group(1) if type_match else ""

        # 右上角角标: tp1=超高价值, tp2=强烈推荐
        product["high_value"] = bool(item.select_one("div.tp1"))
        product["recommend"] = bool(item.select_one("div.tp2"))

        # 计算剩余时间
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

        # 跳过已结束的商品
        if product.get("remain") == "已结束":
            continue

        products.append(product)

    return products, time_period, False


def build_message(products, time_period):
    """构造企业微信 Markdown 消息"""
    lines = [
        f"# {TITLE}",
        f"> **当前时间段**: {time_period}",
        "",
    ]

    for i, p in enumerate(products, 1):
        name = p['name']
        tags = []
        if p.get("recommend"):
            tags.append("🔥强烈推荐")
        if p.get("high_value"):
            tags.append("💎超高价值")
        if tags:
            name = f"{name} <font color=\"warning\">{' '.join(tags)}</font>"
        lines.append(f"**{i}. {name}**")
        lines.append(f"> 价格: <font color=\"warning\">{p['price']}</font> 洛克贝")
        lines.append(f"> 限购: {p['limit']}")
        if p.get("remain"):
            lines.append(f"> ⏳ 剩余时间: <font color=\"info\">{p['remain']}</font>")
        if p["type"]:
            lines.append(f"> 类型: {p['type']}")
        if p["desc"]:
            lines.append(f"> {p['desc']}")
        lines.append("")

    lines.append(f"[📎 点击查看详情]({URL})")
    return "\n".join(lines)


def build_plain_message(products, time_period):
    """构造 Bark/ntfy 使用的纯文本消息"""
    lines = [f"⏰ 当前时间段: {time_period}"]

    for i, p in enumerate(products, 1):
        tags = []
        if p.get("recommend"):
            tags.append("🔥强烈推荐")
        if p.get("high_value"):
            tags.append("💎超高价值")
        flag = f" [{' '.join(tags)}]" if tags else ""

        lines.append(
            f"{i}. {p['name']}{flag} 价格: {p['price']} 洛克贝 | 限购: {p['limit']}"
        )

    return "\n".join(lines)


def build_no_merchant_message():
    """无商人时段的消息"""
    return (
        f"# {TITLE}\n"
        "> **当前时段**: 00:00 - 08:00\n"
        "> ⏰ 每日0:00-8:00无远行商人，请等待下个时间段\n"
        f"\n[📎 点击查看详情]({URL})"
    )


def build_plain_no_merchant_message():
    """无商人时段的纯文本消息"""
    return (
        "⏰ 当前时段: 00:00 - 08:00\n"
        "每日0:00-8:00无远行商人，请等待下个时间段。"
    )


def build_feishu_card(products, time_period):
    """从商品数据直接构建飞书卡片 payload"""
    md_parts = [f"**⏰ 当前时间段**: {time_period}\n"]

    for i, p in enumerate(products, 1):
        tags = []
        if p.get("high_value"):
            tags.append("<font color='orange'>💎超高价值</font>")
        if p.get("recommend"):
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
        md_parts.append("")  # 空行分隔

    md_content = "\n".join(md_parts)

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


def _truncate_bytes(text, max_bytes=3500):
    """按 UTF-8 字节数截断，避免 Bark/ntfy 消息超长"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    suffix = "\n...内容过长，已截断"
    limit = max_bytes - len(suffix.encode("utf-8"))
    return data[:limit].decode("utf-8", "ignore") + suffix


def _retry(fn, retries=3, backoff=1):
    """带指数退避的重试装饰器"""
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            wait = backoff * (2 ** (attempt - 1))
            print(f"⚠️  第{attempt}次失败，{wait}秒后重试: {e}")


def _get_bark_config():
    """返回 (推送地址, 分组, 中断级别)"""
    bark_url = _env("BARK_URL")
    bark_key = _env("BARK_KEY")
    bark_server = _env("BARK_SERVER", "https://api.day.app").rstrip("/")

    if bark_url:
        if "://" not in bark_url:
            bark_url = f"https://{bark_url}"
        endpoint = bark_url.rstrip("/")
    elif bark_key:
        endpoint = f"{bark_server}/{bark_key}"
    else:
        return "", "", ""

    return endpoint, _env("BARK_GROUP"), _env("BARK_LEVEL")


def _get_ntfy_config():
    """返回 (服务地址, 主题, 访问令牌)"""
    ntfy_url = _env("NTFY_URL")
    topic = _env("NTFY_TOPIC")
    server = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    token = _env("NTFY_TOKEN")

    if ntfy_url:
        if "://" not in ntfy_url:
            ntfy_url = f"https://{ntfy_url}"
        parsed = urlsplit(ntfy_url.rstrip("/"))
        path = parsed.path.rstrip("/")
        if path:
            base_path, _, topic = path.rpartition("/")
            server = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        else:
            server = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            if not topic:
                return "", "", ""
    elif not topic:
        return "", "", ""

    if not topic:
        return "", "", ""
    return server, topic, token


def _get_ntfy_priority():
    """读取 ntfy 优先级，范围 1-5，默认 3"""
    try:
        value = int(_env("NTFY_PRIORITY", "3"))
    except ValueError:
        value = 3
    return min(5, max(1, value))


def send_to_feishu(payload):
    """推送飞书卡片消息"""
    if not FEISHU_WEBHOOK_URL:
        print("⏭️  未设置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return

    def _do():
        resp = session.post(FEISHU_WEBHOOK_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ 飞书消息推送成功")
        else:
            print(f"❌ 飞书推送失败: {result}")

    try:
        _retry(_do)
    except Exception as e:
        print(f"❌ 飞书推送异常: {e}")


def send_to_wecom(content):
    """推送消息到企业微信机器人"""
    if not WEBHOOK_URL:
        print("⏭️  未设置 WECOM_WEBHOOK_URL，跳过企业微信推送")
        return

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content,
        },
    }

    def _do():
        resp = session.post(WEBHOOK_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            print("✅ 企业微信消息推送成功")
        else:
            print(f"❌ 企业微信推送失败: {result}")

    try:
        _retry(_do)
    except Exception as e:
        print(f"❌ 企业微信推送异常: {e}")


def send_to_bark(title, content):
    """推送消息到 Bark"""
    endpoint, group, level = _get_bark_config()
    if not endpoint:
        print("⏭️  未设置 BARK_URL / BARK_KEY，跳过 Bark 推送")
        return

    payload = {
        "title": title,
        "body": _truncate_bytes(content),
        "url": URL,
    }
    if group:
        payload["group"] = group
    if level:
        payload["level"] = level

    def _do():
        resp = session.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError:
            result = {}
        if result.get("code", 200) == 200:
            print("✅ Bark 消息推送成功")
        else:
            print(f"❌ Bark 推送失败: {result}")

    try:
        _retry(_do)
    except Exception as e:
        print(f"❌ Bark 推送异常: {e}")


def send_to_ntfy(title, content):
    """推送消息到 ntfy"""
    server, topic, token = _get_ntfy_config()
    if not server:
        print("⏭️  未设置 NTFY_URL / NTFY_TOPIC，跳过 ntfy 推送")
        return

    payload = {
        "topic": topic,
        "title": title,
        "message": _truncate_bytes(content),
        "priority": _get_ntfy_priority(),
        "click": URL,
    }
    tags = [tag.strip() for tag in _env("NTFY_TAGS").split(",") if tag.strip()]
    if tags:
        payload["tags"] = tags

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    endpoint = f"{server.rstrip('/')}/"

    def _do():
        resp = session.post(endpoint, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        print("✅ ntfy 消息推送成功")

    try:
        _retry(_do)
    except Exception as e:
        print(f"❌ ntfy 推送异常: {e}")


def main():
    bark_endpoint, _, _ = _get_bark_config()
    ntfy_server, _, _ = _get_ntfy_config()
    if not any([WEBHOOK_URL, FEISHU_WEBHOOK_URL, bark_endpoint, ntfy_server]):
        print(
            "❌ 错误: 未配置任何推送渠道 "
            "(WECOM_WEBHOOK_URL / FEISHU_WEBHOOK_URL / BARK_URL / BARK_KEY / NTFY_URL / NTFY_TOPIC)"
        )
        sys.exit(1)

    print("🔍 正在抓取远行商人商品信息...")

    try:
        html = fetch_page()
    except Exception as e:
        msg = (
            f"# ⚠️ 洛克王国远行商人\n"
            "> **抓取失败，请检查网站是否可用**\n\n"
            f"**错误信息**: {e}\n"
        )
        feishu_payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "⚠️ 洛克王国远行商人"}, "template": "red"},
                "elements": [{"tag": "markdown", "content": f"**抓取失败**\n\n错误信息: {e}"}],
            },
        }
        plain_msg = f"⚠️ 抓取失败，请检查网站是否可用\n错误信息: {e}"
        send_to_wecom(msg)
        send_to_feishu(feishu_payload)
        send_to_bark(TITLE, plain_msg)
        send_to_ntfy(TITLE, plain_msg)
        return

    products, time_period, is_no_merchant = parse_products(html)

    if is_no_merchant:
        print("⏰ 当前为无商人时段 (0:00-8:00)")
        msg = build_no_merchant_message()
        feishu_payload = build_feishu_no_merchant_card()
        plain_msg = build_plain_no_merchant_message()
    elif not products:
        print("⚠️ 未找到商品信息")
        msg = (
            f"# {TITLE}\n"
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
        plain_msg = "⚠️ 未能获取到商品信息，请手动查看"
    else:
        print(f"📦 当前时间段: {time_period}")
        print(f"📦 找到 {len(products)} 个商品:")
        for p in products:
            tags = []
            if p.get("recommend"):
                tags.append("强烈推荐")
            if p.get("high_value"):
                tags.append("超高价值")
            flag = f" [{'、'.join(tags)}]" if tags else ""
            print(f"   - {p['name']}{flag} | 价格: {p['price']} | 限购: {p['limit']}")
        msg = build_message(products, time_period)
        feishu_payload = build_feishu_card(products, time_period)
        plain_msg = build_plain_message(products, time_period)

    print("\n📨 推送消息预览:")
    print("-" * 40)
    if WEBHOOK_URL or FEISHU_WEBHOOK_URL:
        print("【企业微信 Markdown】")
        print(msg)
        print("-" * 40)
    if bark_endpoint or ntfy_server:
        print("【Bark / ntfy 纯文本】")
        print(plain_msg)
        print("-" * 40)

    # 推送到所有已配置的渠道
    send_to_wecom(msg)
    send_to_feishu(feishu_payload)
    send_to_bark(TITLE, plain_msg)
    send_to_ntfy(TITLE, plain_msg)


if __name__ == "__main__":
    main()