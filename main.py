import os
import json
import time
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from openai import OpenAI

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # 可填频道 ID 或 @ISAYNEWS
GH_TOKEN = os.getenv("GH_TOKEN")

HISTORY_FILE = Path("news_pushed_history.json")
MAX_CANDIDATES = 25
HOURS_WINDOW = 36  # 只考虑近 36 小时内的新闻

# 马来西亚主流媒体 RSS（可按需增减）
RSS_FEEDS = [
    {"name": "Bernama", "url": "https://www.bernama.com/en/rssfeed.php/"},
    {"name": "The Star", "url": "https://www.thestar.com.my/rss/news/nation"},
    {"name": "Free Malaysia Today", "url": "https://www.freemalaysiatoday.com/feed"},
    {"name": "Malaysiakini", "url": "https://www.malaysiakini.com/en/rss"},
]

ALLOWED_CATEGORIES = ["#政治讨论", "#校园新闻", "#经济民生", "#社会热点"]

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
        "pushed_ids": sorted(list(pushed_ids))[-500:],  # 只保留最近 500 条，防止文件过大
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_id(url: str, title: str) -> str:
    """生成稳定去重 ID"""
    raw = (url or "") + "|" + (title or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_entry_time(entry) -> datetime | None:
    """尽量解析 RSS 条目发布时间"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_candidates() -> list[dict]:
    """从多个 RSS 源抓取近期新闻候选"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    candidates = []
    seen_urls = set()

    for source in RSS_FEEDS:
        name = source["name"]
        url = source["url"]
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"[Warn] RSS 解析异常: {name} - {url}")
                continue

            for entry in feed.entries[:15]:
                link = entry.get("link") or ""
                title = (entry.get("title") or "").strip()
                if not title or not link:
                    continue

                # 简单去重（同一 URL）
                if link in seen_urls:
                    continue
                seen_urls.add(link)

                pub_time = parse_entry_time(entry)
                if pub_time and pub_time < cutoff:
                    continue

                summary = entry.get("summary") or entry.get("description") or ""
                # 去掉简单 HTML 标签
                summary = summary.replace("<p>", "").replace("</p>", " ").replace("<br>", " ")
                summary = summary[:600]

                candidates.append({
                    "id": make_id(link, title),
                    "title": title,
                    "link": link,
                    "source": name,
                    "summary": summary,
                    "published": pub_time.isoformat() if pub_time else None,
                })
        except Exception as e:
            print(f"[Error] 抓取 {name} 失败: {e}")

    # 按时间倒序，取前若干条
    candidates.sort(key=lambda x: x.get("published") or "", reverse=True)
    return candidates[:MAX_CANDIDATES]


def generate_telegram_message(candidates: list[dict], client: OpenAI) -> tuple[str | None, str | None]:
    """
    让 AI 从候选新闻中选出当天最重要的一条，并按严格模板输出。
    返回 (message, selected_id)
    """
    if not candidates:
        return None, None

    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    candidates_text = ""
    for i, c in enumerate(candidates, 1):
        candidates_text += (
            f"{i}. 【{c['source']}】{c['title']}\n"
            f"   链接: {c['link']}\n"
            f"   摘要: {c['summary'][:300]}\n\n"
        )

    prompt = f"""你是一位专业的马来西亚新闻编辑，负责为 Telegram 频道挑选并撰写「当天最重要的一条新闻」。

【任务】
1. 从下面候选新闻中，选出**今天对马来西亚最具新闻价值、关注度最高的一条**（只选 1 条）。
2. 将该新闻整理成高质量中文，严格按指定模板输出。
3. 如果原文是英文，请翻译成自然、专业的中文。

【必须严格遵守的输出模板】（不要增减结构，不要输出任何解释）：

**#分类名称**

**新闻主标题（简短有吸引力）**

*{today_str}*

第一段：精炼陈述新闻核心事实（谁、发生了什么、最新进展）。
第二段：补充背景或分析相关延伸影响。

信息来源：[媒体名称1](真实新闻链接) | [媒体名称2](真实新闻链接)

**ISAYNEWS @ISAYNEWS**

【分类只能从以下四个中选一个】：
#政治讨论、#校园新闻、#经济民生、#社会热点

【写作要求】
- 标题要简短、有吸引力，避免过长。
- 第一段只写核心事实，第二段写背景/影响。
- 「信息来源」必须使用候选列表中的真实链接，可写 1~2 个来源。
- 不要编造事实，不要添加模板以外的任何文字。
- 全文使用中文。

【候选新闻列表】：
{candidates_text}

请直接输出最终符合模板的文案（不要前言后语）："""

    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "你是马来西亚新闻编辑。必须严格按用户给出的模板输出，绝不添加额外内容。"
                },
                {"role": "user", "content": prompt}
            ],
            temperature=0.5,
            max_tokens=700
        )

        content = None
        if hasattr(response, "choices") and response.choices:
            choice = response.choices[0]
            if hasattr(choice, "message") and hasattr(choice.message, "content"):
                content = choice.message.content
        elif isinstance(response, dict):
            content = response.get("choices", [{}])[0].get("message", {}).get("content")

        if not content or not isinstance(content, str):
            print("[Warn] AI 返回内容异常")
            return None, None

        content = content.strip()

        # 简单匹配选中的新闻（通过链接是否出现在文案中）
        selected_id = None
        for c in candidates:
            if c["link"] in content:
                selected_id = c["id"]
                break
        if not selected_id and candidates:
            selected_id = candidates[0]["id"]  # 兜底

        return content, selected_id

    except Exception as e:
        print(f"[Error] AI 生成失败: {e}")
        return None, None


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            print("[Success] 消息已推送到 Telegram")
            return True
        print(f"[Error] Telegram 发送失败: {resp.status_code} - {resp.text}")
        # 降级纯文本
        payload["parse_mode"] = None
        resp2 = requests.post(url, json=payload, timeout=20)
        if resp2.status_code == 200:
            print("[Success] 以纯文本模式重新发送成功")
            return True
        return False
    except Exception as e:
        print(f"[Error] Telegram 请求异常: {e}")
        return False


# ==================== 主逻辑 ====================
def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GH_TOKEN]):
        print("[Error] 缺少必要环境变量：TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID / GH_TOKEN")
        return

    print("=== 马来西亚每日重点新闻推送 开始运行 ===")
    print(f"时间: {datetime.now(timezone.utc).isoformat()}")

    client = OpenAI(
        base_url="https://models.github.ai/inference",
        api_key=GH_TOKEN
    )

    pushed_ids = load_history()
    print(f"已推送历史数: {len(pushed_ids)}")

    candidates = fetch_candidates()
    print(f"获取到候选新闻数: {len(candidates)}")

    # 过滤已推送过的
    candidates = [c for c in candidates if c["id"] not in pushed_ids]
    print(f"去重后候选数: {len(candidates)}")

    if not candidates:
        print("今日无新候选新闻，结束。")
        return

    message, selected_id = generate_telegram_message(candidates, client)
    if not message:
        print("[Error] 未能生成有效文案，结束。")
        return

    print("生成文案预览:\n", message[:400], "...")

    success = send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, message)
    if success and selected_id:
        pushed_ids.add(selected_id)
        save_history(pushed_ids)
        print(f"已记录去重 ID: {selected_id}")
    else:
        print("推送失败，未写入历史。")

    print("=== 运行结束 ===")


if __name__ == "__main__":
    main()
