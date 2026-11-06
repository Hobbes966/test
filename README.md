# YouTube 每日摘要

每天早上 7:17（日本时间）自动：
1. 通过 RSS 抓取 `channels.txt` 里频道的新视频（默认跳过 Shorts）
2. 用 Gemini API 直接读取 YouTube 链接生成中文摘要（一句话总结 + 带时间点的要点）
3. 汇总成一封 HTML 邮件发到你的邮箱

全程跑在 GitHub Actions 上，电脑关机也照常运行，免费。

## 部署（约 10 分钟）

### 1. 准备两个密钥
- **Gemini API Key**：在 [Google AI Studio](https://aistudio.google.com/apikey) 创建
- **Gmail 应用专用密码**：Google 账号需先开启两步验证，然后在 [应用专用密码](https://myaccount.google.com/apppasswords) 生成一个 16 位密码（不是你的 Gmail 登录密码）

### 2. 建仓库
新建一个 **私有** GitHub 仓库，把本文件夹全部内容（包括 `.github/` 目录）推上去。

### 3. 填 Secrets
仓库 → Settings → Secrets and variables → Actions → New repository secret：

| 名称 | 值 |
|---|---|
| `GEMINI_API_KEY` | 第 1 步的 Gemini key |
| `SMTP_USER` | 你的 Gmail 地址 |
| `SMTP_PASSWORD` | 第 1 步的 16 位应用专用密码 |
| `MAIL_TO` | 收件地址（可填同一个 Gmail，多个用逗号分隔） |

### 4. 改频道列表
编辑 `channels.txt`，每行一个 `@handle`、频道 ID 或频道网址。

### 5. 手动跑一次测试
仓库 → Actions → Daily YouTube Digest → Run workflow。几分钟后应收到邮件。
手机上给这个发件人设个 Gmail 标签/VIP 通知，就能每天准时弹出。

## 可选配置（Settings → Variables）

| 变量 | 默认 | 说明 |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | 想要更细的摘要可换更强的模型 |
| `SUMMARY_LANG` | `简体中文` | 改成 `日本語` / `English` 即可换语言 |
| `SKIP_SHORTS` | `true` | 设为 `false` 则 Shorts 也总结 |

改运行时间：编辑 `.github/workflows/daily-digest.yml` 里的 cron（UTC 时间，日本时间减 9 小时）。

## 本地测试

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...
DRY_RUN=true python yt_digest.py   # 不发邮件，生成 digest_preview.html 预览
```

## 说明与限制
- 只能总结**公开**视频；首播/直播未结束的视频会失败，次日自动重试，连续失败 3 次后跳过
- Gemini 免费层每天可处理的 YouTube 视频总时长有上限（约 8 小时），关注频道多、视频长时注意
- 只处理最近 72 小时内发布的视频，第一次运行不会把频道的历史视频全部发给你
- `state.json` 记录已处理的视频，由 Actions 自动提交，不要手动删
- GitHub 规定：仓库 60 天没有任何提交时会暂停定时任务；本项目每天提交 `state.json`，正常情况下不会触发
