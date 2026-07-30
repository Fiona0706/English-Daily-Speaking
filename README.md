# English-Daily-Speaking（每日英语口语素材自动生成）

本仓库配合「张张工作台」英语口语板块使用：
GitHub Actions 每天 **北京时间 07:00** 自动调用 DeepSeek 生成新素材（每日5词 / 每日5句 / 每日对话 / 每日2句造句 / 每日跟读文本），
追加进 `content.js` 并生成真人音频到 `audio/en/`，然后提交回本仓库。
CloudStudio 上的工作台运行时从这些文件拉取最新内容，拉不到则自动回退到站内快照兜底。

## 文件说明
- `gen_daily.py` —— 每日生成脚本（调用 DeepSeek + edge-tts）
- `.github/workflows/daily.yml` —— 定时任务（每日 07:00 北京时间）
- `content.js` —— 五大素材池（单一数据源，前端直接引用）
- `audio/en/` —— 真人语音 mp3（edge-tts, en-US-AriaNeural）

## 你需要做（一次性）
1. 在本仓库 **Settings → Secrets and variables → Actions → New repository secret**：
   - Name：`LLM_API_KEY`
   - Value：你的 DeepSeek API Key（`sk-...`）
2. 确认 Actions 已启用：**Settings → Actions → General → Allow all actions**。

## 手动测试
仓库 **Actions → Daily English Content → Run workflow** 即可立即跑一次（无需等到 07:00）。

## 重要提醒
- DeepSeek 账户需有余额（>0）才会放行 API 调用，请先在平台充值。
- 本仓库为 **Public**，但 Key 只存在于加密 Secret，不会出现在代码或公开文件里。
- 生成结果持续提交回 master/main；若某天生成异常，可在 Actions 里查看日志，必要时回退到上一个 commit 的 content.js。
