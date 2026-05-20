from collections import deque
from datetime import datetime
import logging
import threading


console_log_buffer = deque(maxlen=400)
state_lock = threading.Lock()
active_task_id = 0

progress_data = {
    "task_id": 0,
    "total": 0,
    "processed": 0,
    "remaining": 0,
    "start_time": None,
    "end_time": None,
    "is_processing": False,
    "stage": "等待开始",
    "partial_output": "",
    "console_output": "",
}


class TaskReplacedError(Exception):
    """Raised when a newer upload supersedes the current long-running task."""


def _is_task_allowed(task_id):
    return task_id is None or task_id == active_task_id


def is_task_active(task_id):
    with state_lock:
        return _is_task_allowed(task_id)


def ensure_task_active(task_id):
    if not is_task_active(task_id):
        raise TaskReplacedError("检测到新的上传请求，当前任务已中止")


class InMemoryLogHandler(logging.Handler):
    """Copy backend logs into memory so the frontend can poll them."""

    def emit(self, record):
        try:
            console_log_buffer.append(self.format(record))
        except Exception:
            pass


def init_log_capture(app):
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
    global active_task_id
    with state_lock:
        active_task_id += 1
        current_task_id = active_task_id
        progress_data.clear()
        progress_data.update(
            {
                "task_id": current_task_id,
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
        return current_task_id


def update_progress(stage, processed, total, is_processing=True, task_id=None):
    with state_lock:
        if not _is_task_allowed(task_id):
            return False
        progress_data["stage"] = stage
        progress_data["total"] = total
        progress_data["processed"] = processed
        progress_data["remaining"] = max(total - processed, 0)
        progress_data["is_processing"] = is_processing
        if is_processing and not progress_data.get("start_time"):
            progress_data["start_time"] = datetime.now().isoformat()
        if not is_processing:
            progress_data["end_time"] = datetime.now().isoformat()
        return True


def append_console_output(message, logger, task_id=None):
    if not is_task_active(task_id):
        return False
    logger.info(message)
    with state_lock:
        if not _is_task_allowed(task_id):
            return False
        progress_data["console_output"] = get_console_output_text()
        return True


def update_partial_output(text, task_id=None):
    with state_lock:
        if not _is_task_allowed(task_id):
            return False
        progress_data["partial_output"] = (text or "")[-5000:]
        return True


def append_partial_output(text, task_id=None):
    with state_lock:
        if not _is_task_allowed(task_id):
            return False
        existing = progress_data.get("partial_output", "")
        progress_data["partial_output"] = (existing + (text or ""))[-5000:]
        return True


def mark_failed(stage="处理失败", task_id=None):
    with state_lock:
        if not _is_task_allowed(task_id):
            return False
        progress_data["stage"] = stage
        progress_data["is_processing"] = False
        progress_data["end_time"] = datetime.now().isoformat()
        return True


def get_progress_payload():
    with state_lock:
        progress_data["console_output"] = get_console_output_text()
        return dict(progress_data)
