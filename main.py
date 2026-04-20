import logging
import os
import shutil
import sys
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from faster_whisper import WhisperModel
import anthropic
from setproctitle import setproctitle
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

# 每天定时处理的小时（0-23），默认凌晨 1 点
PROCESS_HOUR = int(os.getenv("PROCESS_HOUR", "1"))

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

# 待处理 zip 队列（watchdog 检测到后加入，定时器统一处理）
_pending_zips: list[Path] = []
_pending_lock = threading.Lock()


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def transcribe(model: WhisperModel, audio_path: Path) -> str:
    log.info("转写中: %s", audio_path.name)
    segments, info = model.transcribe(str(audio_path), language=WHISPER_LANGUAGE, beam_size=2)
    log.info("检测语言: %s (概率 %.2f)", info.language, info.language_probability)

    lines = []
    for seg in segments:
        ts = format_timestamp(seg.start)
        lines.append(f"{ts} {seg.text.strip()}")

    transcript = "\n".join(lines)
    log.info("转写完成: %s (%d 行)", audio_path.name, len(lines))
    return transcript


def summarize(client: anthropic.Anthropic, transcript: str) -> str:
    log.info("生成摘要中...")
    resp = client.messages.create(
        model=OPENAI_MODEL,
        max_tokens=4096,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": transcript},
        ],
    )
    text_block = next((b for b in resp.content if hasattr(b, "text") and b.text), None)
    if text_block is None:
        raise ValueError(f"API 响应中无 text block，收到: {[b.type for b in resp.content]}")
    summary = text_block.text
    log.info("摘要完成")
    return summary


def build_transcript_markdown(source_name: str, transcript: str, date: str | None = None) -> str:
    date = date or datetime.now().strftime("%Y-%m-%d")
    stem = Path(source_name).stem
    return f"""---
date: {date}
source: {source_name}
summary_status: pending
---

# {date} {stem}

## 核心摘要

> 摘要生成中...

## 原始转录

{transcript}
"""


def build_full_markdown(source_name: str, summary: str, transcript: str, date: str | None = None) -> str:
    date = date or datetime.now().strftime("%Y-%m-%d")
    stem = Path(source_name).stem
    return f"""---
date: {date}
source: {source_name}
summary_status: done
---

# {date} {stem}

## 核心摘要

{summary}

## 原始转录

{transcript}
"""


def date_from_zip_name(stem: str) -> str:
    """从 zip 文件名提取日期前缀，支持 MMDD（如 0417）或 YYYYMMDD 格式，失败则返回今天。"""
    stem = stem.strip()
    try:
        if len(stem) == 4 and stem.isdigit():          # MMDD
            month, day = int(stem[:2]), int(stem[2:])
            year = datetime.now().year
            return datetime(year, month, day).strftime("%Y-%m-%d")
        elif len(stem) == 8 and stem.isdigit():         # YYYYMMDD
            return datetime.strptime(stem, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def process_zip(zip_path: Path, whisper_model: WhisperModel, llm_client: anthropic.Anthropic):
    log.info("========== 处理 ZIP: %s ==========", zip_path.name)
    file_date = date_from_zip_name(zip_path.stem)
    log.info("文件日期: %s", file_date)
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
        md_filename = f"{file_date}_{audio_file.stem}.md"
        md_path = TRANSCRIPT_DIR / md_filename
        try:
            transcript = transcribe(whisper_model, audio_file)

            # 转写完立即落盘，防止后续步骤失败导致转写内容丢失
            md_path.write_text(build_transcript_markdown(audio_file.name, transcript, file_date), encoding="utf-8")
            log.info("转录已保存: %s", md_path.name)

            summary = summarize(llm_client, transcript)

            # 摘要成功，更新文件为完整版本
            md_path.write_text(build_full_markdown(audio_file.name, summary, transcript, file_date), encoding="utf-8")
            log.info("摘要已更新: %s", md_path.name)
        except Exception:
            log.exception("处理音频文件失败: %s", audio_file.name)
            if md_path.exists():
                log.info("转录文件已保留: %s", md_path.name)

    DONE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(zip_path), str(DONE_DIR / zip_path.name))
        log.info("ZIP 已移至: %s", DONE_DIR / zip_path.name)
    except Exception:
        log.exception("ZIP 移动失败，保留原位: %s", zip_path.name)

    shutil.rmtree(batch_dir, ignore_errors=True)
    log.info("临时目录已清理: %s", batch_dir)
    log.info("========== 处理完成 ==========")


def retry_pending_summaries(llm_client: anthropic.Anthropic):
    """补全 transcript/ 里 summary_status: pending 的文件摘要"""
    pending = [f for f in TRANSCRIPT_DIR.glob("*.md")
               if "summary_status: pending" in f.read_text(encoding="utf-8")]
    if not pending:
        return
    log.info("发现 %d 个待补全摘要的文件", len(pending))
    for md_path in pending:
        log.info("补全摘要: %s", md_path.name)
        try:
            content = md_path.read_text(encoding="utf-8")
            marker = "## 原始转录\n\n"
            idx = content.find(marker)
            if idx == -1:
                log.warning("未找到转录内容，跳过: %s", md_path.name)
                continue
            transcript = content[idx + len(marker):]
            summary = summarize(llm_client, transcript)
            new_content = content.replace("summary_status: pending", "summary_status: done")
            new_content = new_content.replace("> 摘要生成中...", summary)
            md_path.write_text(new_content, encoding="utf-8")
            log.info("摘要已补全: %s", md_path.name)
        except Exception:
            log.exception("补全摘要失败: %s", md_path.name)


def next_process_time() -> datetime:
    """计算下一次处理时间（今天或明天的 PROCESS_HOUR 点）"""
    now = datetime.now()
    target = now.replace(hour=PROCESS_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def scheduled_processor(whisper_model: WhisperModel, llm_client: anthropic.Anthropic):
    """后台线程：每天凌晨 PROCESS_HOUR 点处理队列中的所有 zip"""
    while True:
        try:
            target = next_process_time()
            wait_seconds = (target - datetime.now()).total_seconds()
            log.info("下次处理时间: %s", target.strftime("%Y-%m-%d %H:%M"))

            # 分段 sleep，便于响应退出信号
            while wait_seconds > 0:
                time.sleep(min(wait_seconds, 60))
                wait_seconds = (target - datetime.now()).total_seconds()

            with _pending_lock:
                zips_to_process = [p for p in _pending_zips if p.exists()]
                _pending_zips.clear()

            if zips_to_process:
                log.info("定时处理开始，共 %d 个 ZIP 文件", len(zips_to_process))
                retry_pending_summaries(llm_client)
                for zp in zips_to_process:
                    try:
                        process_zip(zp, whisper_model, llm_client)
                    except Exception:
                        log.exception("处理 ZIP 失败，已跳过: %s", zp.name)
            else:
                log.info("定时检查：无待处理文件，补全摘要中...")
                retry_pending_summaries(llm_client)
        except Exception:
            log.exception("调度线程发生未知错误，60 秒后重试...")
            time.sleep(60)


class ZipHandler(FileSystemEventHandler):
    """
    macOS FSEvents 不支持 on_closed，改用 on_created + on_modified + 防抖定时器。
    文件稳定 5 秒后加入待处理队列，等凌晨定时统一处理。
    """

    def __init__(self):
        self._debounce: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path_str: str):
        with self._lock:
            existing = self._debounce.pop(path_str, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(5.0, self._enqueue, args=[path_str])
            self._debounce[path_str] = timer
            timer.start()

    def _enqueue(self, path_str: str):
        with self._lock:
            self._debounce.pop(path_str, None)
        path = Path(path_str)
        if not path.exists() or path.suffix.lower() != ".zip":
            return
        with _pending_lock:
            if path not in _pending_zips:
                _pending_zips.append(path)
        target = next_process_time()
        log.info("检测到新文件: %s → 已加入队列，将于 %s 处理",
                 path.name, target.strftime("%m-%d %H:%M"))

    def on_created(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".zip":
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() == ".zip":
            self._schedule(event.src_path)


def main():
    setproctitle("auto_transcript")
    log.info("=== Auto Transcript 启动 ===")
    log.info("监听目录: %s", AUDIO_ZIP_DIR)
    log.info("转录输出: %s", TRANSCRIPT_DIR)
    log.info("Whisper 模型: %s", WHISPER_MODEL_NAME)
    log.info("LLM 模型: %s", OPENAI_MODEL)
    log.info("定时处理时间: 每天 %02d:00", PROCESS_HOUR)

    for d in [AUDIO_ZIP_DIR, AUDIO_FILE_DIR, TRANSCRIPT_DIR, DONE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    log.info("加载 Whisper 模型...")
    whisper_model = WhisperModel(WHISPER_MODEL_NAME, device=WHISPER_DEVICE)
    log.info("Whisper 模型加载完成")

    llm_client = anthropic.Anthropic(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

    # 启动时补全未完成的摘要（LLM 轻量操作，立即执行）
    retry_pending_summaries(llm_client)

    # 启动时已有的 zip 加入队列（等凌晨处理）
    existing_zips = sorted(AUDIO_ZIP_DIR.glob("*.zip"))
    if existing_zips:
        with _pending_lock:
            for zp in existing_zips:
                if zp not in _pending_zips:
                    _pending_zips.append(zp)
        target = next_process_time()
        log.info("发现 %d 个待处理 ZIP，将于 %s 处理", len(existing_zips), target.strftime("%m-%d %H:%M"))

    # 启动定时处理后台线程
    processor = threading.Thread(
        target=scheduled_processor,
        args=(whisper_model, llm_client),
        daemon=True,
        name="scheduler",
    )
    processor.start()

    # 启动 watchdog 监听
    handler = ZipHandler()
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


def run_now():
    """立即处理 audio_zip/ 里所有 zip，处理完退出。供 agent 或手动调用。"""
    log.info("=== Auto Transcript 立即处理模式 ===")
    for d in [AUDIO_ZIP_DIR, AUDIO_FILE_DIR, TRANSCRIPT_DIR, DONE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    log.info("加载 Whisper 模型...")
    whisper_model = WhisperModel(WHISPER_MODEL_NAME, device=WHISPER_DEVICE)
    log.info("Whisper 模型加载完成")
    llm_client = anthropic.Anthropic(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)

    retry_pending_summaries(llm_client)

    zips = sorted(AUDIO_ZIP_DIR.glob("*.zip"))
    if not zips:
        log.info("audio_zip/ 中无待处理文件")
        return
    log.info("找到 %d 个 ZIP，开始逐一处理", len(zips))
    for zp in zips:
        try:
            process_zip(zp, whisper_model, llm_client)
        except Exception:
            log.exception("处理失败，跳过: %s", zp.name)
    log.info("=== 全部处理完成 ===")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        run_now()
    else:
        main()
