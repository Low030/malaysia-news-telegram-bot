# 马来西亚每日新闻 Telegram 推送机器人

自动抓取马来西亚主流媒体新闻，转为简体中文，按固定排版推送到 Telegram 频道（支持配图、多篇推送）。

## 功能特点

- 多源 RSS：Bernama、The Star、FMT、Malaysiakini、Malay Mail、Google News MY 等
- 每次运行最多推送 3 篇（可调）
- 每天多次定时运行，更接近即时更新
- 英文/马来文自动转简体中文
- 固定 HTML 排版
- 自动配图 + 去重

## 输出格式

```text
中文标题（粗体）
#每日新闻 #分类

日期（粗体斜体下划线）

中文正文

信息来源：媒体名（可点击）

尾缀（粗体）
```

分类可选：#政治讨论 #校园新闻 #经济民生 #社会热点

## Secrets 配置

| Secret | 说明 |
|--------|------|
| TELEGRAM_BOT_TOKEN | Bot Token |
| TELEGRAM_CHANNEL_ID | 频道数字 ID |
| GH_TOKEN | GitHub PAT（models:read） |
| MESSAGE_FOOTER | 可选尾缀，默认“每日要闻” |

## 运行频率

- 自动：UTC 0/4/8/12/16 点（约北京时间 08/12/16/20/00）
- 手动：Actions 页面触发
- 环境变量 MAX_PUSH_PER_RUN 控制每轮篇数（默认 3）

## 文件

- main.py：核心逻辑
- requirements.txt：依赖
- .github/workflows/daily_run.yml：定时任务
- news_pushed_history.json：去重记录
