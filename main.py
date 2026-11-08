import os
import json
import re
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import unescape

import feedparser
import requests

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GH_TOKEN = os.getenv("GH_TOKEN")
MESSAGE_FOOTER = os.getenv("MESSAGE_FOOTER") or "**每日要闻**"

HISTORY_FILE = Path("news_pushed_history.json")
MAX_CANDIDATES = 20
HOURS_WINDOW = 36
AI_CANDIDATE_LIMIT = 8  # 送给 AI 的候选数量，避免 prompt 过长

RSS_FEEDS = [
    {"name": "Bernama", "url": "https://www.bernama.com/en/rssfeed.php/"},
    {"name": "The Star", "url": "https://www.thestar.com.my/rss/news/nation"},
    {"name": "Free Malaysia Today", "url": "https://www.freemalaysiatoday.com/feed"},
    {"name": "Malaysiakini", "url": "https://www.malaysiakini.com/en/rss"},
]

# GitHub Models 可用端点
MODEL_ENDPOINTS = [
    "https://models.github.ai/inference/chat/completions",
    "https://models.inference.ai.azure.com/chat/completions",
]
MODEL_NAMES = ["openai/gpt-4o-mini", "gpt-4o-mini"]


# ==================== 工具函数 ====================
def load_history() -> set:
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("pushed_ids", []))
        except Exception:
            return set()
    return set()


def save_history(pushed_ids: set):
    data = {
        "pushed_ids": sorted(list(pushed_ids))[-500:],
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_id(url: str, title: str) -> str:
    raw = (url or "") + "|" + (title or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_image_url(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        if isinstance(items, list):
            for item in items:
                url = item.get("url") if isinstance(item, dict) else None
                if url and url.startswith("http"):
                    return url

    for enc in entry.get("enclosures") or []:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and ("image" in typ or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href

    html = entry.get("summary") or entry.get("description") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if m:
        url = m.group(1)
        if url.startswith("http"):
            return url
    return None


def fetch_candidates() -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    candidates = []
    seen_urls = set()

    for source in RSS_FEEDS:
        name = source["name"]
        url = source["url"]
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"[Warn] RSS 解析异常: {name}")
                continue

            for entry in feed.entries[:15]:
                link = entry.get("link") or ""
                title = (entry.get("title") or "").strip()
                if not title or not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                pub_time = parse_entry_time(entry)
                if pub_time and pub_time < cutoff:
                    continue

                summary = strip_html(entry.get("summary") or entry.get("description") or "")[:600]
                image_url = extract_image_url(entry)

                candidates.append({
                    "id": make_id(link, title),
                    "title": title,
                    "link": link,
                    "source": name,
                    "summary": summary,
                    "image_url": image_url,
                    "published": pub_time.isoformat() if pub_time else None,
                })
        except Exception as e:
            print(f"[Error] 抓取 {name} 失败: {e}")

    candidates.sort(key=lambda x: x.get("published") or "", reverse=True)
    return candidates[:MAX_CANDIDATES]


def guess_category(title: str, summary: str) -> str:
    text = (title + " " + summary).lower()
    if any(k in text for k in ["school", "student", "university", "campus", "spM", "教育", "校园", "学生"]):
        return "#校园新闻"
    if any(k in text for k in ["economy", "bank", "inflation", "subsidy", "price", "job", "经济", "民生", "油价", "薪"]):
        return "#经济民生"
    if any(k in text for k in ["minister", "prime minister", "parliament", "election", "party", "政治", "首相", "部长", "选举", "政党"]):
        return "#政治讨论"
    return "#社会热点"


def is_valid_message(text: str) -> bool:
    """拒绝 AI 返回的 OK / 空内容 / 过短内容"""
    if not text:
        return False
    t = text.strip()
    if len(t) < 60:
        return False
    if t.upper() in {"OK", "OK.", "YES", "SUCCESS", "DONE"}:
        return False
    # 至少要有分类或来源痕迹
    if "信息来源" not in t and "**#" not in t and "#政治" not in t and "#社会" not in t and "#经济" not in t and "#校园" not in t:
        return False
    return True


def build_fallback_message(item: dict) -> str:
    """AI 失败时的本地模板（保证频道一定有可用内容）"""
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    category = guess_category(item.get("title", ""), item.get("summary", ""))
    title = item.get("title", "今日要闻").strip()
    summary = item.get("summary") or "详情请点击原文链接阅读。"
    # 摘要过长时截断
    if len(summary) > 220:
        summary = summary[:220].rstrip() + "…"
    source = item.get("source", "媒体")
    link = item.get("link", "")

    return (
        f"**{category}**\n\n"
        f"**{title}**\n\n"
        f"*{today_str}*\n\n"
        f"{summary}\n\n"
        f"本条为系统根据公开报道整理的重点资讯，建议结合原文了解完整细节与后续进展。\n\n"
        f"信息来源：[{source}]({link})\n\n"
        f"{MESSAGE_FOOTER}"
    )


def call_github_models(prompt: str) -> str | None:
    """直接用 HTTP 调用 GitHub Models，避免 SDK 兼容问题，并打印真实返回"""
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    for endpoint in MODEL_ENDPOINTS:
        for model in MODEL_NAMES:
            payload = {
                "model": model,
                "temperature": 0.4,
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是马来西亚中文新闻编辑。你只能输出完整新闻文案，"
                            "禁止只回复 OK、Yes、Done 或任何短确认语。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                print(f"[Info] 调用模型: {model} @ {endpoint}")
                resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
                print(f"[Info] HTTP {resp.status_code}")
                if resp.status_code != 200:
                    print(f"[Warn] 接口错误: {resp.text[:400]}")
                    continue

                data = resp.json()
                content = None
                try:
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    content = data.get("output_text") or data.get("content")

                if isinstance(content, list):
                    # 某些接口返回 content blocks
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("text"):
                            parts.append(block["text"])
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts)

                if content and isinstance(content, str):
                    content = content.strip()
                    print(f"[Info] AI 原文前 120 字: {content[:120]!r}")
                    if is_valid_message(content):
                        return content
                    print("[Warn] AI 返回内容未通过校验，继续尝试其他模型/端点")
                else:
                    print(f"[Warn] 无法解析 content，keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
            except Exception as e:
                print(f"[Error] 调用失败: {type(e).__name__}: {e}")

    return None


def generate_telegram_message(candidates: list):
    """返回 (message, selected_id, image_url)"""
    if not candidates:
        return None, None, None

    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    short_list = candidates[:AI_CANDIDATE_LIMIT]

    candidates_text = ""
    for i, c in enumerate(short_list, 1):
        candidates_text += (
            f"{i}. 【{c['source']}】{c['title']}\n"
            f"   链接: {c['link']}\n"
            f"   摘要: {c['summary'][:220]}\n\n"
        )

    prompt = f"""请从下列候选新闻中选出今天对马来西亚最重要的一条，并输出完整中文推送文案。

【输出模板，必须完整遵守】

**#分类名称**

**新闻主标题**

*{today_str}*

第一段：核心事实（谁、发生了什么、最新进展）。

第二段：背景或影响。

信息来源：[媒体名称](真实链接)

{MESSAGE_FOOTER}

【分类只能选一个】#政治讨论 / #校园新闻 / #经济民生 / #社会热点

【硬性要求】
1. 必须输出完整文案，禁止只回复 OK、Yes、Done。
2. 必须包含「信息来源」和真实链接。
3. 英文请翻译成中文。
4. 不要输出模板以外的说明。

【候选新闻】
{candidates_text}
"""

    ai_text = call_github_models(prompt)

    if ai_text and is_valid_message(ai_text):
        selected = short_list[0]
        for c in short_list:
            if c["link"] in ai_text:
                selected = c
                break
        return ai_text, selected["id"], selected.get("image_url")

    # AI 失败 → 本地兜底，保证有实质内容
    print("[Warn] AI 不可用或返回无效内容，启用本地模板兜底")
    selected = short_list[0]
    # 优先选有图的候选
    for c in short_list:
        if c.get("image_url"):
            selected = c
            break
    return build_fallback_message(selected), selected["id"], selected.get("image_url")


def send_text_only(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            print("[Success] 纯文本消息已推送")
            return True
        print(f"[Error] sendMessage 失败: {resp.status_code} - {resp.text[:300]}")
        payload["parse_mode"] = None
        resp2 = requests.post(url, json=payload, timeout=20)
        if resp2.status_code == 200:
            print("[Success] 纯文本（无 Markdown）推送成功")
            return True
        return False
    except Exception as e:
        print(f"[Error] Telegram 请求异常: {e}")
        return False


def send_telegram_message(token: str, chat_id: str, text: str, image_url: str | None = None) -> bool:
    if not is_valid_message(text):
        print("[Error] 拒绝发送无效文案:", repr(text[:80]))
        return False

    caption = text if len(text) <= 1024 else (text[:1000] + "…")

    if image_url:
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "Markdown",
            }
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                print("[Success] 已带图推送到 Telegram")
                if len(text) > 1024:
                    send_text_only(token, chat_id, text)
                return True
            print(f"[Warn] sendPhoto 失败: {resp.status_code} - {resp.text[:300]}")
            payload["parse_mode"] = None
            resp2 = requests.post(url, json=payload, timeout=30)
            if resp2.status_code == 200:
                print("[Success] 带图（纯文本 caption）推送成功")
                return True
        except Exception as e:
            print(f"[Warn] sendPhoto 异常: {e}")

    return send_text_only(token, chat_id, text)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GH_TOKEN]):
        print("[Error] 缺少必要环境变量：TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID / GH_TOKEN")
        return

    print("=== 马来西亚每日重点新闻推送 开始运行 ===")
    print(f"时间: {datetime.now(timezone.utc).isoformat()}")

    pushed_ids = load_history()
    print(f"已推送历史数: {len(pushed_ids)}")

    candidates = fetch_candidates()
    print(f"获取到候选新闻数: {len(candidates)}")
    print(f"其中带配图候选: {sum(1 for c in candidates if c.get('image_url'))}")

    candidates = [c for c in candidates if c["id"] not in pushed_ids]
    print(f"去重后候选数: {len(candidates)}")

    if not candidates:
        print("今日无新候选新闻，结束。")
        return

    message, selected_id, image_url = generate_telegram_message(candidates)
    if not message:
        print("[Error] 未能生成有效文案，结束。")
        return

    print("生成文案预览:\n", message[:450], "...")
    print(f"配图: {image_url or '无'}")

    success = send_telegram_message(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHANNEL_ID,
        message,
        image_url=image_url,
    )
    if success and selected_id:
        pushed_ids.add(selected_id)
        save_history(pushed_ids)
        print(f"已记录去重 ID: {selected_id}")
    else:
        print("推送失败，未写入历史。")

    print("=== 运行结束 ===")


if __name__ == "__main__":
    main()
