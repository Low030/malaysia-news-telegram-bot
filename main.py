import os
import json
import re
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import unescape

import feedparser
import requests
from openai import OpenAI

# ==================== 配置 ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GH_TOKEN = os.getenv("GH_TOKEN")
# 文案尾缀放在 Secret 中，避免仓库暴露频道信息；未配置时使用中性默认
MESSAGE_FOOTER = os.getenv("MESSAGE_FOOTER", "**每日要闻**")

HISTORY_FILE = Path("news_pushed_history.json")
MAX_CANDIDATES = 25
HOURS_WINDOW = 36

RSS_FEEDS = [
    {"name": "Bernama", "url": "https://www.bernama.com/en/rssfeed.php/"},
    {"name": "The Star", "url": "https://www.thestar.com.my/rss/news/nation"},
    {"name": "Free Malaysia Today", "url": "https://www.freemalaysiatoday.com/feed"},
    {"name": "Malaysiakini", "url": "https://www.malaysiakini.com/en/rss"},
]

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
    """从 RSS 条目中尽量提取配图 URL"""
    # media:content / media:thumbnail
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        if isinstance(items, list):
            for item in items:
                url = item.get("url") if isinstance(item, dict) else None
                if url and url.startswith("http"):
                    return url

    # enclosure
    enclosures = entry.get("enclosures") or []
    for enc in enclosures:
        href = enc.get("href") or enc.get("url")
        typ = (enc.get("type") or "").lower()
        if href and ("image" in typ or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return href

    # HTML 中的 <img src="...">
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


def extract_ai_text(response) -> str | None:
    """兼容多种 OpenAI / GitHub Models 返回结构"""
    if response is None:
        return None
    if isinstance(response, str):
        return response.strip() or None

    # 标准 ChatCompletion
    try:
        choices = getattr(response, "choices", None)
        if choices:
            msg = choices[0].message
            content = getattr(msg, "content", None)
            if content:
                return str(content).strip()
    except Exception:
        pass

    # dict 形态
    if isinstance(response, dict):
        try:
            content = response["choices"][0]["message"]["content"]
            if content:
                return str(content).strip()
        except Exception:
            pass
        # 有些接口直接返回 output_text
        if response.get("output_text"):
            return str(response["output_text"]).strip()

    return None


def generate_telegram_message(candidates: list, client: OpenAI):
    """返回 (message, selected_id, image_url)"""
    if not candidates:
        return None, None, None

    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    candidates_text = ""
    for i, c in enumerate(candidates, 1):
        candidates_text += (
            f"{i}. 【{c['source']}】{c['title']}\n"
            f"   链接: {c['link']}\n"
            f"   摘要: {c['summary'][:280]}\n\n"
        )

    prompt = f"""你是一位专业的马来西亚新闻编辑，负责挑选并撰写「当天最重要的一条新闻」。

【任务】
1. 从候选新闻中选出今天对马来西亚最具新闻价值的一条（只选 1 条）。
2. 整理成高质量中文，严格按模板输出。
3. 英文请翻译成自然专业的中文。

【必须严格遵守的输出模板】（不要增减结构，不要输出任何解释）：

**#分类名称**

**新闻主标题（简短有吸引力）**

*{today_str}*

第一段：精炼陈述新闻核心事实（谁、发生了什么、最新进展）。
第二段：补充背景或分析相关延伸影响。

信息来源：[媒体名称1](真实新闻链接) | [媒体名称2](真实新闻链接)

{MESSAGE_FOOTER}

【分类只能从以下四个中选一个】：
#政治讨论、#校园新闻、#经济民生、#社会热点

【写作要求】
- 标题简短有吸引力。
- 第一段写核心事实，第二段写背景/影响。
- 「信息来源」必须使用候选列表中的真实链接，可写 1~2 个。
- 不要编造事实，不要添加模板以外的文字。
- 全文使用中文。

【候选新闻列表】：
{candidates_text}

请直接输出最终符合模板的文案："""

    models_to_try = ["openai/gpt-4o-mini", "gpt-4o-mini"]

    for model_name in models_to_try:
        try:
            print(f"[Info] 尝试模型: {model_name}")
            response = client.chat.completions.create(
                model=model_name,
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
            content = extract_ai_text(response)
            if content:
                selected_id = None
                image_url = None
                for c in candidates:
                    if c["link"] in content:
                        selected_id = c["id"]
                        image_url = c.get("image_url")
                        break
                if not selected_id:
                    selected_id = candidates[0]["id"]
                    image_url = candidates[0].get("image_url")
                return content, selected_id, image_url

            print(f"[Warn] 模型 {model_name} 返回空内容，type={type(response)}")
            # 打印有限调试信息
            try:
                print(f"[Debug] response repr: {repr(response)[:500]}")
            except Exception:
                pass
        except Exception as e:
            print(f"[Error] 模型 {model_name} 调用失败: {type(e).__name__}: {e}")

    return None, None, None


def send_telegram_message(token: str, chat_id: str, text: str, image_url: str | None = None) -> bool:
    """优先带图发送（sendPhoto），失败则回退纯文本"""
    # Telegram caption 最长 1024
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
                # 若全文超过 caption 限制，再补发一条完整文字
                if len(text) > 1024:
                    send_text_only(token, chat_id, text)
                return True
            print(f"[Warn] sendPhoto 失败: {resp.status_code} - {resp.text[:300]}")
            # 再试一次无 Markdown
            payload["parse_mode"] = None
            resp2 = requests.post(url, json=payload, timeout=30)
            if resp2.status_code == 200:
                print("[Success] 带图（纯文本 caption）推送成功")
                return True
        except Exception as e:
            print(f"[Warn] sendPhoto 异常: {e}")

    return send_text_only(token, chat_id, text)


def send_text_only(token: str, chat_id: str, text: str) -> bool:
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
    with_image = sum(1 for c in candidates if c.get("image_url"))
    print(f"其中带配图候选: {with_image}")

    candidates = [c for c in candidates if c["id"] not in pushed_ids]
    print(f"去重后候选数: {len(candidates)}")

    if not candidates:
        print("今日无新候选新闻，结束。")
        return

    message, selected_id, image_url = generate_telegram_message(candidates, client)
    if not message:
        print("[Error] 未能生成有效文案，结束。")
        return

    print("生成文案预览:\n", message[:400], "...")
    if image_url:
        print(f"配图: {image_url}")
    else:
        print("未找到配图，将发送纯文本")

    success = send_telegram_message(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHANNEL_ID,
        message,
        image_url=image_url
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
