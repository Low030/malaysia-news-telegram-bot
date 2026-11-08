import os
import json
import re
import hashlib
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import unescape, escape

import feedparser
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GH_TOKEN = os.getenv("GH_TOKEN")
MESSAGE_FOOTER = (os.getenv("MESSAGE_FOOTER") or "<b>每日要闻</b>").strip()

HISTORY_FILE = Path("news_pushed_history.json")
MAX_CANDIDATES = 20
HOURS_WINDOW = 36
AI_CANDIDATE_LIMIT = 6

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
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"})


def load_history() -> set:
    if not HISTORY_FILE.exists():
        return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("pushed_ids", []))
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
    return hashlib.sha256(f"{url or ''}|{title or ''}".encode("utf-8")).hexdigest()[:24]


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
    return re.sub(r"\s+", " ", text).strip()


def extract_image_url(entry):
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key) or []:
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
    if m and m.group(1).startswith("http"):
        return m.group(1)
    return None


def fetch_candidates() -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    candidates, seen = [], set()
    for source in RSS_FEEDS:
        name, url = source["name"], source["url"]
        try:
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
                if not title or not link or link in seen:
                    continue
                seen.add(link)
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


def guess_category(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    if any(k in text for k in ["school", "student", "university", "campus", "教育", "校园", "学生"]):
        return "#校园新闻"
    if any(k in text for k in ["economy", "bank", "inflation", "subsidy", "price", "job", "经济", "民生", "油价"]):
        return "#经济民生"
    if any(k in text for k in ["minister", "prime minister", "parliament", "election", "party", "政治", "首相", "部长", "选举"]):
        return "#政治讨论"
    return "#社会热点"


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def is_valid_message(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 40:
        return False
    if t.upper() in {"OK", "OK.", "YES", "SUCCESS", "DONE"}:
        return False
    if "信息来源" not in t:
        return False
    if not contains_cjk(t):
        return False
    return True


def format_html_message(title_zh: str, category: str, body_zh: str, source: str, link: str) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    title_zh = strip_html(title_zh).strip() or "今日要闻"
    body_zh = strip_html(body_zh).strip()
    if len(body_zh) > 400:
        body_zh = body_zh[:400].rstrip() + "…"
    for j in ["本条为系统根据公开报道整理的重点资讯", "建议结合原文了解完整细节与后续进展"]:
        body_zh = body_zh.replace(j, "")
    body_zh = re.sub(r"\n{3,}", "\n\n", body_zh).strip()

    return (
        f"<b>{escape(title_zh)}</b>\n"
        f"{escape(category)}\n"
        f"<b><i><u>{today_str}</u></i></b>\n\n"
        f"{escape(body_zh)}\n\n"
        f"信息来源：<a href=\"{escape(link, quote=True)}\">{escape(source)}</a>\n\n"
        f"{MESSAGE_FOOTER}"
    )


def call_github_models(prompt: str, max_tokens: int = 700):
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
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是马来西亚中文新闻编辑。只输出中文内容，禁止回复 OK/Yes/Done，禁止输出英文正文。",
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                print(f"[Info] 调用模型: {model}")
                resp = SESSION.post(endpoint, headers=headers, json=payload, timeout=60)
                print(f"[Info] HTTP {resp.status_code}")
                if resp.status_code != 200:
                    print(f"[Warn] 接口错误: {resp.text[:250]}")
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
                    print(f"[Info] AI 前80字: {content[:80]!r}")
                    if content and content.upper() not in {"OK", "YES", "DONE"} and contains_cjk(content):
                        return content
                    print("[Warn] AI 返回无效/非中文")
            except Exception as e:
                print(f"[Warn] 模型异常: {e}")
    return None


def ai_rewrite_one(item: dict):
    prompt = f"""请把下面这则新闻改写成中文推送素材。

标题: {item['title']}
来源: {item['source']}
链接: {item['link']}
摘要: {item['summary'][:350]}

请严格按下面 3 行格式输出（不要多写）：
分类:#政治讨论 或 #校园新闻 或 #经济民生 或 #社会热点
标题:中文标题（简短有力）
正文:两段中文，第一段核心事实，第二段背景影响。不要出现「本条为系统」这类套话。
"""
    raw = call_github_models(prompt, max_tokens=500)
    if not raw:
        return None

    category, title_zh, body_zh = "#社会热点", "", ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("分类:"):
            category = line.replace("分类:", "", 1).strip() or category
        elif line.startswith("标题:"):
            title_zh = line.replace("标题:", "", 1).strip()
        elif line.startswith("正文:"):
            body_zh = line.replace("正文:", "", 1).strip()

    if not title_zh or not body_zh:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not title_zh and lines:
            title_zh = re.sub(r"^标题[:：]\s*", "", lines[0])
        if not body_zh and len(lines) > 1:
            body_zh = "\n".join(lines[1:])

    if not contains_cjk(title_zh) or not contains_cjk(body_zh):
        return None
    if category not in {"#政治讨论", "#校园新闻", "#经济民生", "#社会热点"}:
        category = guess_category(item["title"], item["summary"])
    return title_zh, category, body_zh


def build_fallback_message(item: dict) -> str:
    category = guess_category(item.get("title", ""), item.get("summary", ""))
    title = item.get("title") or "今日要闻"
    summary = item.get("summary") or ""

    if contains_cjk(title) and contains_cjk(summary):
        title_zh, body_zh = title, summary
    elif contains_cjk(title):
        title_zh = title
        body_zh = summary if contains_cjk(summary) else f"据 {item.get('source', '媒体')} 报道，相关事件仍在持续发展中，详情见原文。"
    else:
        title_zh = f"国际要闻：{title[:40]}" if len(title) > 40 else f"国际要闻：{title}"
        body_zh = (
            f"据 {item.get('source', '外媒')} 报道，{title}。"
            + (f" {summary[:160]}" if summary else "")
            + " 完整细节请查看原文链接。"
        )

    return format_html_message(
        title_zh=title_zh,
        category=category,
        body_zh=body_zh,
        source=item.get("source", "媒体"),
        link=item.get("link", ""),
    )


def generate_telegram_message(candidates: list):
    if not candidates:
        return None, None, None

    short_list = candidates[:AI_CANDIDATE_LIMIT]
    ordered = sorted(short_list, key=lambda x: (0 if x.get("image_url") else 1))

    for item in ordered[:3]:
        rewritten = ai_rewrite_one(item)
        if rewritten:
            title_zh, category, body_zh = rewritten
            msg = format_html_message(
                title_zh=title_zh,
                category=category,
                body_zh=body_zh,
                source=item.get("source", "媒体"),
                link=item.get("link", ""),
            )
            if is_valid_message(msg):
                return msg, item["id"], item.get("image_url")

    print("[Warn] AI 中文改写失败，使用本地模板")
    selected = ordered[0]
    return build_fallback_message(selected), selected["id"], selected.get("image_url")


def send_text_only(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for parse_mode in ("HTML", None):
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
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
        print("[Error] 拒绝发送无效文案:", repr(text[:100]))
        return False

    caption = text if len(text) <= 1024 else text[:1000] + "…"
    if image_url:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        for parse_mode in ("HTML", None):
            payload = {"chat_id": chat_id, "photo": image_url, "caption": caption}
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


def main() -> int:
    try:
        if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GH_TOKEN]):
            print("[Error] 缺少 Secrets: TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID / GH_TOKEN")
            return 0

        print("=== 马来西亚每日重点新闻推送 开始 ===")
        print(f"时间: {datetime.now(timezone.utc).isoformat()}")

        pushed_ids = load_history()
        print(f"已推送历史数: {len(pushed_ids)}")

        candidates = fetch_candidates()
        print(f"候选新闻数: {len(candidates)} | 带图: {sum(1 for c in candidates if c.get('image_url'))}")

        candidates = [c for c in candidates if c["id"] not in pushed_ids]
        print(f"去重后: {len(candidates)}")
        if not candidates:
            print("今日无新候选，正常结束。")
            return 0

        message, selected_id, image_url = generate_telegram_message(candidates)
        if not message:
            print("[Error] 文案生成失败")
            return 0

        print("文案预览:\n", message[:500], "...")
        print(f"配图: {image_url or '无'}")

        success = send_telegram_message(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, message, image_url=image_url
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
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
