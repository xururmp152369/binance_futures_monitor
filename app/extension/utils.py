import logging
import os

async def send_chunked(message, lines: list[str], *, max_len: int = 4096) -> None:
    """將多行清單分批發送，超過 max_len 字元時自動切分並標示（N/M）。"""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        cost = len(line) + (1 if current else 0)  # +1 for joining \n
        if current_len + cost > max_len:
            chunks.append(current)
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += cost
    if current:
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        suffix = f"\n\n`（{i}/{total}）`" if total > 1 else ""
        await message.reply_text("\n".join(chunk) + suffix, parse_mode="Markdown")

# ================== LOG 設定：同時輸出到控制台 + 檔案 ==================
def setup_logging():
    """建立並回傳專案共用 logger。

    預設輸出到 stdout（Docker 可用 `docker logs` 查看）。
    若環境變數 `LOG_TO_FILE=1`，則另外寫入 `_codeExecution.log`。
    """
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