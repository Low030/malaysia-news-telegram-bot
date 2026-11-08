# 马来西亚每日重点新闻 → Telegram 推送机器人

每天自动从马来西亚主流媒体抓取新闻，使用 AI 选出当天最重要的一条，按固定排版推送到 Telegram 频道（支持配图）。

## 功能特点

- 多源 RSS 抓取（Bernama、The Star、Free Malaysia Today、Malaysiakini 等）
- AI 自动筛选「当天最重要」一条新闻
- 英文新闻自动高质量翻译为中文
- 严格按指定 Markdown 模板输出
- 自动提取新闻配图并随消息发送
- 本地 `news_pushed_history.json` 去重，避免重复推送
- 每天定时运行 + 支持手动触发

## 输出格式示例

```
**#政治讨论**

**新闻主标题**

*2026年11月08日*

第一段核心事实……

第二段背景与影响……

信息来源：[媒体A](链接) | [媒体B](链接)

**每日要闻**
```

可选分类：`#政治讨论` `#校园新闻` `#经济民生` `#社会热点`

## 配置步骤

### 1. 添加 GitHub Secrets

进入仓库 **Settings → Secrets and variables → Actions**，添加：

| Secret 名称 | 说明 |
|-------------|------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHANNEL_ID` | 频道数字 ID（推荐 `-100xxxxxxxxxx`） |
| `GH_TOKEN` | GitHub PAT（需 `models:read` 权限） |
| `MESSAGE_FOOTER` | （可选）文案固定尾缀，例如你的品牌签名；不填则用默认「每日要闻」 |

### 2. Telegram 频道设置

1. 把 Bot 加入目标频道并设为管理员（发送消息权限）
2. 获取频道数字 ID，填入 `TELEGRAM_CHANNEL_ID`

### 3. 运行

- 每天 **UTC 02:00（北京时间 10:00）** 自动运行
- 也可在 **Actions** 页面手动运行 workflow

## 文件说明

| 文件 | 说明 |
|------|------|
| `main.py` | 核心逻辑 |
| `requirements.txt` | Python 依赖 |
| `.github/workflows/daily_run.yml` | 定时 + 手动触发 |
| `news_pushed_history.json` | 已推送新闻去重记录 |

## 工作流程简述

1. 从多个媒体 RSS 拉取近 36 小时新闻并提取配图
2. 过滤已推送过的新闻
3. 调用 GitHub Models 选出最重要一条并按模板生成中文文案
4. 优先 `sendPhoto` 带图推送，失败则回退纯文本
5. 更新 `news_pushed_history.json` 并自动 commit 回仓库

## 注意事项

- 请确保 `GH_TOKEN` 拥有 `models:read` 权限
- RSS 源可能偶尔失效，可在 `main.py` 的 `RSS_FEEDS` 中自行增减
- 每天只推送 **1 条** 最重点新闻
- 部分新闻源可能没有配图，此时会发送纯文本
