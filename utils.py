import logging
import os


def _split_text(text: str, max_len: int):
    if text is None:
        return [""]
    if len(text) <= max_len:
        return [text]

    chunks = []
    buf = ""
    for line in text.splitlines(True):
        if len(buf) + len(line) <= max_len:
            buf += line
            continue

        if buf:
            chunks.append(buf)
            buf = ""

        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]

        buf = line

    if buf:
        chunks.append(buf)
    return chunks


async def reply_text_long(message, text: str, *, max_len: int = 3500, **kwargs):
    for chunk in _split_text(text, max_len=max_len):
        await message.reply_text(chunk, **kwargs)


# ================== LOG 設定：同時輸出到控制台 + 檔案 ==================
def setup_logging():
    log_to_file = os.getenv("LOG_TO_FILE", "0").strip().lower() in {"1", "true", "yes", "y"}

    handlers = [logging.StreamHandler()]
    if log_to_file:
        handlers.insert(0, logging.FileHandler('_codeExecution.log', encoding='utf-8'))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=handlers
    )
    
    # 過濾 Telegram Bot API 的 HTTP 請求日誌
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.WARNING)
    
    return logging.getLogger()