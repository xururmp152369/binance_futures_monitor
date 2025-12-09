import logging

# ================== LOG 設定：同時輸出到控制台 + 檔案 ==================
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.FileHandler('_codeExecution.log', encoding='utf-8'),
            logging.StreamHandler()  # 輸出到控制台
        ]
    )
    
    # 過濾 Telegram Bot API 的 HTTP 請求日誌
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('telegram.ext').setLevel(logging.WARNING)
    
    return logging.getLogger()