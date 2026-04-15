import logging
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from faster_whisper import WhisperModel
from openai import OpenAI
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_transcript")

AUDIO_ZIP_DIR = Path(os.getenv("AUDIO_ZIP_DIR", "")).expanduser()
AUDIO_FILE_DIR = Path(os.getenv("AUDIO_FILE_DIR", "")).expanduser()
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "")).expanduser()
DONE_DIR = Path(os.getenv("DONE_DIR", "")).expanduser()

WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "MiniMax-M2.7-highspeed")

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".mp4", ".aac", ".flac", ".ogg", ".wma"}

SUMMARY_SYSTEM_PROMPT = """你是一位专业的投资研究助手，擅长从会议录音、路演和访谈中提炼投资相关信息。
请将以下转录文字整理为结构化的投资笔记，用中文输出，包含以下三个部分：

### 核心观点
（列出3-5条最重要的观点或判断）

### 关键数据
（列出所有具体数字、比率、规模、时间节点等）

### 行动项
（以 - [ ] 格式列出需要跟进的事项）

保持客观，忠实于原文，不要添加原文没有的内容。"""


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def transcribe(model: WhisperModel, audio_path: Path) -> str:
    log.info("转写中: %s", audio_path.name)
    segments, info = model.transcribe(str(audio_path), language=WHISPER_LANGUAGE)
    log.info("检测语言: %s (概率 %.2f)", info.language, info.language_probability)

    lines = []
    for seg in segments:
        ts = format_timestamp(seg.start)
        lines.append(f"{ts} {seg.text.strip()}")

    transcript = "\n".join(lines)
    log.info("转写完成: %s (%d 行)", audio_path.name, len(lines))
    return transcript


def summarize(client: OpenAI, transcript: str) -> str:
    log.info("生成摘要中...")
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    summary = resp.choices[0].message.content
    log.info("摘要完成")
    return summary


def build_markdown(source_name: str, summary: str, transcript: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    stem = Path(source_name).stem
    return f"""---
date: {today}
source: {source_name}
---

# {today} {stem}

## 核心摘要

{summary}

## 原始转录

{transcript}
"""


def process_zip(zip_path: Path, whisper_model: WhisperModel, llm_client: OpenAI):
    log.info("========== 处理 ZIP: %s ==========", zip_path.name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = AUDIO_FILE_DIR / f"{timestamp}_{zip_path.stem}"

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(batch_dir)
        log.info("解压到: %s", batch_dir)
    except zipfile.BadZipFile:
        log.error("无效的 ZIP 文件: %s", zip_path.name)
        return

    audio_files = sorted(
        f for f in batch_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS and not f.name.startswith(".")
    )

    if not audio_files:
        log.warning("ZIP 中未找到音频文件: %s", zip_path.name)
    else:
        log.info("找到 %d 个音频文件", len(audio_files))

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    for audio_file in audio_files:
        try:
            transcript = transcribe(whisper_model, audio_file)
            summary = summarize(llm_client, transcript)
            md_content = build_markdown(audio_file.name, summary, transcript)

            today = datetime.now().strftime("%Y-%m-%d")
            md_filename = f"{today}_{audio_file.stem}.md"
            md_path = TRANSCRIPT_DIR / md_filename
            md_path.write_text(md_content, encoding="utf-8")
            log.info("Markdown 已保存: %s", md_path.name)
        except Exception:
            log.exception("处理音频文件失败: %s", audio_file.name)

    DONE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(zip_path), str(DONE_DIR / zip_path.name))
    log.info("ZIP 已移至: %s", DONE_DIR / zip_path.name)

    shutil.rmtree(batch_dir, ignore_errors=True)
    log.info("临时目录已清理: %s", batch_dir)
    log.info("========== 处理完成 ==========")


class ZipHandler(FileSystemEventHandler):
    def __init__(self, whisper_model: WhisperModel, llm_client: OpenAI):
        self.whisper_model = whisper_model
        self.llm_client = llm_client

    def on_closed(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".zip":
            return
        time.sleep(2)
        process_zip(path, self.whisper_model, self.llm_client)


def main():
    log.info("=== Auto Transcript 启动 ===")
    log.info("监听目录: %s", AUDIO_ZIP_DIR)
    log.info("转录输出: %s", TRANSCRIPT_DIR)
    log.info("Whisper 模型: %s", WHISPER_MODEL_NAME)
    log.info("LLM 模型: %s", OPENAI_MODEL)

    for d in [AUDIO_ZIP_DIR, AUDIO_FILE_DIR, TRANSCRIPT_DIR, DONE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    log.info("加载 Whisper 模型...")
    whisper_model = WhisperModel(WHISPER_MODEL_NAME, device=WHISPER_DEVICE)
    log.info("Whisper 模型加载完成")

    llm_client = OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

    # 启动时处理 audio_zip/ 中已有的 zip 文件
    existing_zips = sorted(AUDIO_ZIP_DIR.glob("*.zip"))
    if existing_zips:
        log.info("发现 %d 个待处理的 ZIP 文件", len(existing_zips))
        for zp in existing_zips:
            process_zip(zp, whisper_model, llm_client)

    handler = ZipHandler(whisper_model, llm_client)
    observer = Observer()
    observer.schedule(handler, str(AUDIO_ZIP_DIR), recursive=False)
    observer.start()
    log.info("Watchdog 监听已启动，等待新文件...")

    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        log.info("收到中断信号，停止监听...")
        observer.stop()
    observer.join()
    log.info("=== Auto Transcript 已停止 ===")


if __name__ == "__main__":
    main()
