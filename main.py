import os
import json
import re
import hashlib
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import unescape

import feedparser
import requests

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GH_TOKEN = os.getenv("GH_TOKEN")
MESSAGE_FOOTER = (os.getenv("MESSAGE_FOOTER") or "**每日要闻**").strip()

HISTORY_FILE = Path("news_pushed_history.json")
MAX_CANDIDATES = 20
HOURS_WINDOW = 36
AI_CANDIDATE_LIMIT = 8

RSS_FEEDS = [
    {"name": "Bernama", "url": "https://www.bernama.com/en/rssfeed.php/"},
    {"name": "The Star", "url": "https://www.thestar.com.my/rss/news/nation"},
    {"name": "Free Malaysia Today", "url": "https://www.freemalaysiatoday.com/feed"},
    {"name": "Malaysiakini", "url": "https://www.malaysiakini.com/en/rss"},
]

MODEL_ENDPOINTS = [
    "https://models.github.ai/inference/chat/completions",
    "https://models.inference.ai.azure.com/chat/completions",
]
MODEL_NAMES = ["openai/gpt-4o-mini", "gpt-4o-mini"]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)",
})


# ==================== 历史记录 ====================
def load_history() -> set:
    if not HISTORY_FILE.exists():
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("pushed_ids", []))
    except Exception as e:
        print(f"[Warn] 读取历史失败: {e}")
        return set()


def save_history(pushed_ids: set) -> None:
    data = {
        "pushed_ids": sorted(list(pushed_ids))[-500:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_id(url: str, title: str) -> str:
    raw = f"{url or ''}|{title or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ==================== RSS ====================
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


def extract_image_url(entry):
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    url = item.get("url")
                    if url and str(url).startswith("http"):
                        return url

    for enc in entry.get("enclosures") or []:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and ("image" in typ or str(href).lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
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
            # 先用 requests 拉取，再交给 feedparser，提高稳定性
            resp = SESSION.get(url, timeout=20)
            if resp.status_code != 200:
                print(f"[Warn] RSS HTTP {resp.status_code}: {name}")
                continue
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                print(f"[Warn] RSS 无条目: {name}")
                continue

            for entry in feed.entries[:15]:
                link = (entry.get("link") or "").strip()
                title = (entry.get("title") or "").strip()
                if not title or not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                pub_time = parse_entry_time(entry)
                if pub_time and pub_time < cutoff:
                    continue

                summary = strip_html(entry.get("summary") or entry.get("description") or "")[:600]
                candidates.append({
                    "id": make_id(link, title),
                    "title": title,
                    "link": link,
                    "source": name,
                    "summary": summary,
                    "image_url": extract_image_url(entry),
                    "published": pub_time.isoformat() if pub_time else None,
                })
            print(f"[Info] {name}: 解析成功")
        except Exception as e:
            print(f"[Warn] 抓取失败 {name}: {e}")

    candidates.sort(key=lambda x: x.get("published") or "", reverse=True)
    return candidates[:MAX_CANDIDATES]


# ==================== 文案 ====================
def guess_category(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    if any(k in text for k in ["school", "student", "university", "campus", "教育", "校园", "学生"]):
        return "#校园新闻"
    if any(k in text for k in ["economy", "bank", "inflation", "subsidy", "price", "job", "经济", "民生", "油价"]):
        return "#经济民生"
    if any(k in text for k in ["minister", "prime minister", "parliament", "election", "party", "政治", "首相", "部长", "选举"]):
        return "#政治讨论"
    return "#社会热点"


def is_valid_message(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 60:
        return False
    if t.upper() in {"OK", "OK.", "YES", "SUCCESS", "DONE"}:
        return False
    markers = ("信息来源", "**#", "#政治", "#社会", "#经济", "#校园")
    return any(m in t for m in markers)


def build_fallback_message(item: dict) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    category = guess_category(item.get("title", ""), item.get("summary", ""))
    title = (item.get("title") or "今日要闻").strip()
    summary = item.get("summary") or "详情请点击原文链接阅读。"
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


def call_github_models(prompt: str):
    if not GH_TOKEN:
        return None

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
                            "你是马来西亚中文新闻编辑。只能输出完整新闻文案，"
                            "禁止只回复 OK、Yes、Done 或任何短确认语。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                print(f"[Info] 调用模型: {model}")
                resp = SESSION.post(endpoint, headers=headers, json=payload, timeout=60)
                print(f"[Info] HTTP {resp.status_code}")
                if resp.status_code != 200:
                    print(f"[Warn] 接口错误: {resp.text[:300]}")
                    continue

                data = resp.json()
                content = None
                try:
                    content = data["choices"][0]["message"]["content"]
                except Exception:
                    content = data.get("output_text") or data.get("content")

                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("text"):
                            parts.append(block["text"])
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts)

                if isinstance(content, str):
                    content = content.strip()
                    print(f"[Info] AI 原文前 100 字: {content[:100]!r}")
                    if is_valid_message(content):
                        return content
                    print("[Warn] AI 内容未通过校验")
            except Exception as e:
                print(f"[Warn] 模型调用异常: {e}")

    return None


def generate_telegram_message(candidates: list):
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

    print("[Warn] AI 不可用，启用本地模板兜底")
    selected = short_list[0]
    for c in short_list:
        if c.get("image_url"):
            selected = c
            break
    return build_fallback_message(selected), selected["id"], selected.get("image_url")


# ==================== Telegram ====================
def send_text_only(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for parse_mode in ("Markdown", None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = SESSION.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                print(f"[Success] 文本推送成功 parse_mode={parse_mode}")
                return True
            print(f"[Warn] sendMessage {resp.status_code}: {resp.text[:250]}")
        except Exception as e:
            print(f"[Warn] sendMessage 异常: {e}")
    return False


def send_telegram_message(token: str, chat_id: str, text: str, image_url=None) -> bool:
    if not is_valid_message(text):
        print("[Error] 拒绝发送无效文案:", repr(text[:80]))
        return False

    caption = text if len(text) <= 1024 else text[:1000] + "…"

    if image_url:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        for parse_mode in ("Markdown", None):
            payload = {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": caption,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                resp = SESSION.post(url, json=payload, timeout=30)
                if resp.status_code == 200:
                    print("[Success] 带图推送成功")
                    if len(text) > 1024:
                        send_text_only(token, chat_id, text)
                    return True
                print(f"[Warn] sendPhoto {resp.status_code}: {resp.text[:250]}")
            except Exception as e:
                print(f"[Warn] sendPhoto 异常: {e}")

    return send_text_only(token, chat_id, text)


# ==================== 主流程 ====================
def main() -> int:
    try:
        if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GH_TOKEN]):
            print("[Error] 缺少 Secrets: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID / GH_TOKEN")
            return 0  # 不让 workflow 因配置问题标红也可，但这里返回 0 表示流程走完

        print("=== 马来西亚每日重点新闻推送 开始 ===")
        print(f"时间: {datetime.now(timezone.utc).isoformat()}")

        pushed_ids = load_history()
        print(f"已推送历史数: {len(pushed_ids)}")

        candidates = fetch_candidates()
        print(f"候选新闻数: {len(candidates)}")
        print(f"带配图: {sum(1 for c in candidates if c.get('image_url'))}")

        candidates = [c for c in candidates if c["id"] not in pushed_ids]
        print(f"去重后: {len(candidates)}")

        if not candidates:
            print("今日无新候选，正常结束。")
            return 0

        message, selected_id, image_url = generate_telegram_message(candidates)
        if not message:
            print("[Error] 文案生成失败")
            return 0

        print("文案预览:\n", message[:450], "...")
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
            print("推送失败，未写入历史")

        print("=== 运行结束 ===")
        return 0
    except Exception as e:
        print(f"[Error] 未捕获异常: {e}")
        traceback.print_exc()
        # 返回 0，避免偶发网络问题把整个 workflow 标成失败；关键错误已打印
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
