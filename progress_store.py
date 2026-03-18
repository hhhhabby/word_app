from collections import deque
from datetime import datetime
import logging


console_log_buffer = deque(maxlen=400)
progress_data = {
    "total": 0,
    "processed": 0,
    "remaining": 0,
    "start_time": None,
    "end_time": None,
    "is_processing": False,
    "partial_output": "",
    "console_output": "",
}


class InMemoryLogHandler(logging.Handler):
    """将后端日志同步到内存，供前端轮询。"""

    def emit(self, record):
        try:
            message = self.format(record)
            console_log_buffer.append(message)
        except Exception:
            pass


def init_log_capture(app):
    """初始化日志捕获，包含 werkzeug 请求日志。"""
    handler = InMemoryLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    for logger_name in ("werkzeug", app.logger.name):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(h, InMemoryLogHandler) for h in logger.handlers):
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def get_console_output_text():
    return "\n".join(console_log_buffer)


def reset_progress(total=0, stage="准备开始", is_processing=False):
    progress_data.clear()
    progress_data.update(
        {
            "total": total,
            "processed": 0,
            "remaining": total,
            "start_time": datetime.now().isoformat() if is_processing else None,
            "end_time": None,
            "is_processing": is_processing,
            "stage": stage,
            "partial_output": "",
            "console_output": get_console_output_text(),
        }
    )


def update_progress(stage, processed, total, is_processing=True):
    progress_data["stage"] = stage
    progress_data["total"] = total
    progress_data["processed"] = processed
    progress_data["remaining"] = max(total - processed, 0)
    progress_data["is_processing"] = is_processing
    if is_processing and not progress_data.get("start_time"):
        progress_data["start_time"] = datetime.now().isoformat()
    if not is_processing:
        progress_data["end_time"] = datetime.now().isoformat()


def append_console_output(message, logger):
    logger.info(message)
    progress_data["console_output"] = get_console_output_text()


def update_partial_output(text):
    max_len = 5000
    progress_data["partial_output"] = (text or "")[-max_len:]


def append_partial_output(text):
    max_len = 5000
    existing = progress_data.get("partial_output", "")
    progress_data["partial_output"] = (existing + (text or ""))[-max_len:]


def mark_failed():
    progress_data["is_processing"] = False
    progress_data["end_time"] = datetime.now().isoformat()


def get_progress_payload():
    progress_data["console_output"] = get_console_output_text()
    return progress_data
