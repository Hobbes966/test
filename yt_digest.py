#!/usr/bin/env python3
"""
YouTube 频道每日摘要
RSS 抓取关注频道的新视频 → Gemini API 直接读取 YouTube 链接生成摘要 → 汇总成一封邮件发送
"""
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import markdown
from google import genai
from google.genai import types

ROOT = Path(__file__).parent
CHANNELS_FILE = ROOT / "channels.txt"
STATE_FILE = ROOT / "state.json"
PREVIEW_FILE = ROOT / "digest_preview.html"

# ---------- 配置（都可以用环境变量覆盖） ----------
MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
SUMMARY_LANG = os.getenv("SUMMARY_LANG") or "简体中文"
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS") or 72)       # 只处理这段时间内发布的视频
MAX_VIDEOS = int(os.getenv("MAX_VIDEOS_PER_RUN") or 15)        # 每次最多总结几条，防止爆额度
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS") or 3)             # 单条视频失败几次后放弃
SKIP_SHORTS = (os.getenv("SKIP_SHORTS") or "true").lower() == "true"
SEND_EMPTY = (os.getenv("SEND_EMPTY") or "false").lower() == "true"
DRY_RUN = (os.getenv("DRY_RUN") or "false").lower() == "true"  # true：只生成预览文件，不发邮件、不写状态

SMTP_HOST = os.getenv("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.getenv("SMTP_PORT") or 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_TO = os.getenv("MAIL_TO") or SMTP_USER

JST = timezone(timedelta(hours=9))
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+1",
}

PROMPT = """你是一名视频内容编辑。请完整观看这个 YouTube 视频，用{lang}写一份摘要。
频道：{channel}
标题：{title}

按以下 Markdown 格式输出，不要客套话，不要复述标题：

**一句话总结**：……

**要点**
- （3–6 条，每条开头标注大致时间点，如 `[05:12]`）

**值得记下的细节**
- （具体的数据、观点、推荐的产品/资源等；没有就省略本节）
"""


def log(msg):
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def http_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# ---------- 状态 ----------
def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text("utf-8") or "{}")
    else:
        state = {}
    state.setdefault("processed", [])     # 已处理过的视频 ID
    state.setdefault("failures", {})      # 视频 ID -> 失败次数
    state.setdefault("channel_ids", {})   # @handle -> UC… 缓存
    return state


def save_state(state):
    state["processed"] = state["processed"][-2000:]
    state["last_run"] = datetime.now(JST).isoformat(timespec="seconds")  # 保证每天都有提交，避免 GitHub 60 天停用定时任务
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")


# ---------- 频道 ----------
def read_channel_list():
    entries = []
    for line in CHANNELS_FILE.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def resolve_channel_id(entry, cache):
    """支持 UC… 频道 ID、@handle、频道网址。"""
    if re.fullmatch(r"UC[\w-]{22}", entry):
        return entry
    m = re.search(r"/channel/(UC[\w-]{22})", entry)
    if m:
        return m.group(1)
    if entry in cache:
        return cache[entry]

    if entry.startswith("http"):
        url = entry
    else:
        url = f"https://www.youtube.com/{entry if entry.startswith('@') else '@' + entry}"
    page = http_get(url)
    for pattern in (r'"externalId":"(UC[\w-]{22})"',
                    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                    r'"browseId":"(UC[\w-]{22})"'):
        m = re.search(pattern, page)
        if m:
            cache[entry] = m.group(1)
            return m.group(1)
    raise RuntimeError(f"找不到频道 ID：{entry}")


def fetch_feed(channel_id):
    xml = http_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    root = ET.fromstring(xml)
    channel_name = root.findtext("a:title", default=channel_id, namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        link = e.find("a:link", NS)
        href = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        videos.append({
            "id": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "channel": channel_name,
            "published": datetime.fromisoformat(e.findtext("a:published", namespaces=NS)),
            "is_short": "/shorts/" in href,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return videos


# ---------- Gemini ----------
def summarize(client, video):
    prompt = PROMPT.format(lang=SUMMARY_LANG, channel=video["channel"], title=video["title"])
    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=types.Content(parts=[
                    types.Part(file_data=types.FileData(file_uri=video["url"])),
                    types.Part(text=prompt),
                ]),
                # 低分辨率抽帧：摘要主要靠语音内容，能大幅节省 token 和免费额度
                config=types.GenerateContentConfig(
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                ),
            )
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("Gemini 返回了空内容")
            return text
        except Exception as e:  # 429 / 5xx 等，退避重试
            last_err = e
            wait = 20 * (attempt + 1)
            log(f"  第 {attempt + 1} 次失败：{e}；{wait}s 后重试")
            time.sleep(wait)
    raise last_err


# ---------- 邮件 ----------
def build_email_html(done, failed):
    today = datetime.now(JST).strftime("%Y-%m-%d")
    parts = [f"""<div style="max-width:680px;margin:0 auto;font-family:-apple-system,'PingFang SC','Hiragino Sans',sans-serif;color:#222;line-height:1.6">
<h2 style="margin:0 0 4px">YouTube 每日摘要</h2>
<p style="margin:0 0 20px;color:#888;font-size:13px">{today} · {len(done)} 个新视频</p>"""]
    for v in done:
        body = markdown.markdown(v["summary"], extensions=["extra"])
        pub = v["published"].astimezone(JST).strftime("%m-%d %H:%M")
        parts.append(f"""<div style="border-top:1px solid #eee;padding:18px 0">
<a href="{v['url']}"><img src="https://i.ytimg.com/vi/{v['id']}/mqdefault.jpg" width="100%" style="max-width:320px;border-radius:8px;display:block" alt=""></a>
<h3 style="margin:10px 0 2px;font-size:17px"><a href="{v['url']}" style="color:#111;text-decoration:none">{escape(v['title'])}</a></h3>
<p style="margin:0 0 8px;color:#888;font-size:13px">{escape(v['channel'])} · {pub}</p>
<div style="font-size:15px">{body}</div>
</div>""")
    if failed:
        items = "".join(f'<li><a href="{v["url"]}">{escape(v["title"])}</a>（{escape(v["channel"])}）</li>'
                        for v in failed)
        parts.append(f"""<div style="border-top:1px solid #eee;padding:14px 0;color:#888;font-size:13px">
<p style="margin:0">以下视频暂时无法总结（可能是首播/直播未结束或额度不足），明天会再试：</p><ul>{items}</ul></div>""")
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, html):
    if not (SMTP_USER and SMTP_PASSWORD and MAIL_TO):
        raise RuntimeError("缺少 SMTP_USER / SMTP_PASSWORD / MAIL_TO")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(SMTP_USER, [a.strip() for a in MAIL_TO.split(",")], msg.as_string())


# ---------- 主流程 ----------
def main():
    state = load_state()
    processed = set(state["processed"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    # 1. 收集新视频
    candidates = []
    for entry in read_channel_list():
        try:
            cid = resolve_channel_id(entry, state["channel_ids"])
            videos = fetch_feed(cid)
        except Exception as e:
            log(f"频道 {entry} 抓取失败：{e}")
            continue
        new = [v for v in videos
               if v["id"] not in processed
               and v["published"] >= cutoff
               and not (SKIP_SHORTS and v["is_short"])]
        log(f"{entry} → {len(new)} 个新视频")
        candidates.extend(new)

    candidates.sort(key=lambda v: v["published"])
    candidates = candidates[:MAX_VIDEOS]

    if not candidates and not SEND_EMPTY:
        log("没有新视频，结束。")
        if not DRY_RUN:
            save_state(state)
        return

    # 2. 逐个总结
    client = genai.Client()  # 读取环境变量 GEMINI_API_KEY
    done, failed = [], []
    for v in candidates:
        log(f"总结：[{v['channel']}] {v['title']}")
        try:
            v["summary"] = summarize(client, v)
            done.append(v)
        except Exception as e:
            log(f"  放弃本次：{e}")
            failed.append(v)

    # 3. 发邮件
    if done or failed or SEND_EMPTY:
        subject = f"YouTube 摘要 {datetime.now(JST):%m/%d} · {len(done)} 个新视频"
        html = build_email_html(done, failed)
        if DRY_RUN:
            PREVIEW_FILE.write_text(html, "utf-8")
            log(f"DRY_RUN：已写入 {PREVIEW_FILE.name}，未发送邮件")
            return
        send_email(subject, html)
        log(f"邮件已发送至 {MAIL_TO}")

    # 4. 邮件发出后才更新状态，避免丢消息
    for v in done:
        state["processed"].append(v["id"])
        state["failures"].pop(v["id"], None)
    for v in failed:
        n = state["failures"].get(v["id"], 0) + 1
        if n >= MAX_ATTEMPTS:
            state["processed"].append(v["id"])
            state["failures"].pop(v["id"], None)
        else:
            state["failures"][v["id"]] = n
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"运行失败：{e}")
        sys.exit(1)
