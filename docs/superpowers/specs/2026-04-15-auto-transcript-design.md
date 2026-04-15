# Auto Transcript — 设计文档

**日期：** 2026-04-15  
**状态：** 已确认，待实现

---

## Context

用户通过 iOS 录制音频后，原本经飞书传给 OpenClaw Gateway 做转写和摘要。但飞书和 Gateway 均有 30MB 文件大小上限，部分音频超限无法处理。

本项目的目标是在 macOS 本地搭建一套全自动流水线：iOS 将音频打包成 zip 上传到 iCloud，Mac 自动检测、解压、转写、摘要，最终输出 Obsidian 风格的 Markdown 文件，绕开大小限制，减少手动操作。

---

## 技术选型

| 组件 | 选型 |
|------|------|
| 文件监听 | `watchdog`（事件驱动，比 cron 更即时） |
| 转写 | `faster-whisper`，本地模型 `mobiuslabsgmbh/faster-whisper-large-v3-turbo` |
| 摘要 | OpenAI-compatible SDK，接 OpenClaw Gateway |
| 配置管理 | `.env` + `python-dotenv` |
| 后台常驻 | macOS LaunchAgent（`.plist`） |

---

## 目录结构

### 项目代码

```
/Users/hz/Dev/auto_transcript/
├── main.py                          # 主脚本（全部逻辑）
├── .env                             # 本地配置（不提交 git）
├── .env.example                     # 配置模板
├── requirements.txt                 # Python 依赖
├── com.hz.auto-transcript.plist     # LaunchAgent 配置
└── docs/superpowers/specs/          # 设计文档
```

### iCloud 工作目录

```
~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio/
├── audio_zip/          # iOS 上传 zip 到此处
├── audio_file/         # 解压后的临时音频（按批次子目录）
│   └── transcript/     # 输出的 Markdown 文件
└── audio_zip_done/     # 处理完成的 zip 归档
```

---

## 处理流程

```
[watchdog 持续监听 audio_zip/]
        │
        ▼  on_closed 事件（zip 写入完成）
[1. 解压]
   解压到 audio_file/{YYYYMMDD_HHMMSS}_{原文件名}/
        │
        ▼  遍历子目录内每个音频文件
[2. 转写 · faster-whisper large-v3-turbo]
   language=zh，输出带时间轴的分段文本
   格式：[HH:MM:SS] 文本内容
        │
        ▼
[3. 摘要 · OpenClaw Gateway / MiniMax-M2.7-highspeed]
   System prompt：投资人视角，中文输出
   输出：核心观点 / 关键数据 / 行动项
        │
        ▼
[4. 写入 Markdown]
   文件名：YYYY-MM-DD_{原音频文件名}.md
   保存到：audio_file/transcript/
        │
        ▼
[5. 清理]
   zip 移入 audio_zip_done/
   删除 audio_file/{批次}/ 临时目录
```

**关键细节：**
- 使用 `on_closed` 而非 `on_created`，确保 zip 完整落盘后再处理
- 每批次用时间戳子目录隔离，支持多 zip 并发
- 音频格式支持：`.m4a` `.mp3` `.wav` `.mp4` `.aac`（faster-whisper 原生支持）

---

## Markdown 输出格式

```markdown
---
date: 2026-04-15
source: 原音频文件名.m4a
---

# 2026-04-15 原音频文件名

## 核心摘要

### 核心观点
- ...

### 关键数据
- ...

### 行动项
- [ ] ...

## 原始转录

[00:00:00] 文本内容...
[00:01:23] 文本内容...
```

**说明：**
- frontmatter 支持 Obsidian 按日期筛选
- `行动项` 使用 `- [ ]`，Obsidian 可直接打勾
- 时间轴格式 `[HH:MM:SS]`，每个 faster-whisper segment 一行
- 每个音频文件单独生成一个 Markdown

---

## 配置文件（.env）

```env
# OpenClaw Gateway
OPENAI_BASE_URL=https://v2.aicodee.com
OPENAI_API_KEY=<在本地 .env 中填写，不提交 git>
OPENAI_MODEL=MiniMax-M2.7-highspeed

# 路径
AUDIO_ZIP_DIR=~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio/audio_zip
AUDIO_FILE_DIR=~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio/audio_file
TRANSCRIPT_DIR=~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio/audio_file/transcript
DONE_DIR=~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio/audio_zip_done

# Whisper
WHISPER_MODEL=mobiuslabsgmbh/faster-whisper-large-v3-turbo
WHISPER_DEVICE=auto
WHISPER_LANGUAGE=zh
```

---

## 依赖（requirements.txt）

```
watchdog
faster-whisper
openai
python-dotenv
```

---

## 摘要 System Prompt

```
你是一位专业的投资研究助手，擅长从会议录音、路演和访谈中提炼投资相关信息。
请将以下转录文字整理为结构化的投资笔记，用中文输出，包含以下三个部分：

### 核心观点
（列出3-5条最重要的观点或判断）

### 关键数据
（列出所有具体数字、比率、规模、时间节点等）

### 行动项
（以 - [ ] 格式列出需要跟进的事项）

保持客观，忠实于原文，不要添加原文没有的内容。
```

---

## LaunchAgent 配置

文件路径：`~/Library/LaunchAgents/com.hz.auto-transcript.plist`

- 开机自动启动
- 崩溃自动重启（`KeepAlive: true`）
- 日志输出到 `~/Library/Logs/auto-transcript.log`

---

## 验证方案

1. **手动测试：** `python main.py` 启动后，手动复制一个 zip 到 `audio_zip/`，观察是否自动处理并在 `transcript/` 生成 Markdown
2. **LaunchAgent 测试：** `launchctl load` 后重启 Mac，确认进程自动运行
3. **大文件测试：** 放入超过 30MB 的音频 zip，验证全流程正常
4. **并发测试：** 同时放入两个 zip，验证互不干扰
