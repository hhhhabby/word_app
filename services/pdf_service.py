import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from io import BytesIO

from openai import OpenAI
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError

from progress_store import (
    TaskReplacedError,
    append_console_output,
    append_partial_output,
    ensure_task_active,
    is_task_active,
    update_partial_output,
)


EXCEL_COLUMNS = ["Unit", "英文单词/短语", "词性", "中文释义"]
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def infer_provider(api_key):
    """Use the key prefix to choose a model provider."""
    key = (api_key or "").strip().lower()
    return "deepseek" if key.startswith("sk-") else "doubao"


def validate_deepseek_key(api_key):
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    response = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        temperature=0,
        max_tokens=8,
        messages=[
            {"role": "system", "content": "你是健康检查助手，只返回 OK。"},
            {"role": "user", "content": "请返回 OK"},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    if "OK" not in content.upper():
        raise ValueError("密钥验证失败")


def validate_doubao_key(api_key):
    model_id = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-lite-260215")
    client = OpenAI(api_key=api_key, base_url=DOUBAO_BASE_URL)
    try:
        if hasattr(client, "responses"):
            response = client.responses.create(model=model_id, input="请仅返回 OK")
            content = extract_responses_text(response).strip()
        else:
            response = client.chat.completions.create(
                model=model_id,
                temperature=0,
                messages=[{"role": "user", "content": "请仅返回 OK"}],
            )
            content = (response.choices[0].message.content or "").strip()
        if "OK" not in content.upper():
            raise ValueError("密钥验证失败")
    except Exception as exc:
        raise ValueError(f"方舟校验失败（model={model_id}）：{exc}")


def parse_pdf_with_deepseek(pdf_bytes, api_key, logger, task_id=None):
    """Extract PDF text and ask DeepSeek to return structured JSON rows."""
    poppler_path = find_poppler_path()
    ensure_task_active(task_id)
    pdf_text = extract_pdf_text(pdf_bytes, poppler_path)
    if not pdf_text:
        current_env = (os.getenv("POPPLER_PATH") or "").strip() or "<未设置>"
        current_which = shutil.which("pdfinfo") or "<未找到>"
        raise ValueError(
            "PDF 未提取到可识别文本。请确认 Poppler 可用，且 PDF 不是纯图片扫描件。"
            f" 当前 POPPLER_PATH={current_env}，which(pdfinfo)={current_which}"
        )

    max_chars = int(os.getenv("PDF_MAX_TEXT_CHARS", "120000"))
    pdf_text = pdf_text[:max_chars]
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    content = stream_chat_completion_text(
        client,
        os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        build_text_pdf_messages(pdf_text),
        logger,
        temperature=0,
        task_id=task_id,
    )
    rows = json.loads(strip_code_fence(content))
    validate_rows_schema(rows)
    return rows


def parse_pdf_with_doubao(pdf_bytes, api_key, logger, task_id=None):
    """Convert PDF pages to images and ask Doubao Vision to extract rows."""
    poppler_path = find_poppler_path()
    ensure_task_active(task_id)
    image_urls = convert_pdf_to_data_urls(pdf_bytes, poppler_path)
    if not image_urls:
        raise ValueError("PDF 转图片失败，无法提交豆包多模态识别")

    chat_content = [{"type": "text", "text": build_image_pdf_prompt()}]
    for url in image_urls:
        chat_content.append({"type": "image_url", "image_url": {"url": url}})

    model_id = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-lite-260215")
    client = OpenAI(api_key=api_key, base_url=DOUBAO_BASE_URL)
    try:
        content = stream_chat_completion_text(
            client,
            model_id,
            [{"role": "user", "content": chat_content}],
            logger,
            temperature=0,
            task_id=task_id,
        )
    except TaskReplacedError:
        raise
    except Exception as exc:
        raise ValueError(f"豆包调用失败（model={model_id}）：{exc}")

    rows = json.loads(strip_code_fence(content))
    validate_rows_schema(rows)
    return rows


def stream_chat_completion_text(client, model, messages, logger, temperature=0, task_id=None):
    """Stream model output while updating the frontend progress panel."""
    chunks = []
    started_at = time.time()
    stop_event = threading.Event()
    stream_holder = {"stream": None}

    def heartbeat():
        while not stop_event.wait(1.0):
            if task_id is not None and not is_task_active(task_id):
                stream = stream_holder.get("stream")
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                break
            elapsed = int(time.time() - started_at)
            append_partial_output(f"\n[系统] 模型处理中，已等待 {elapsed} 秒...", task_id=task_id)
            append_console_output(f"模型处理中，已等待 {elapsed} 秒", logger, task_id=task_id)

    threading.Thread(target=heartbeat, daemon=True).start()

    ensure_task_active(task_id)
    update_partial_output("[系统] 已发起流式请求，等待模型返回内容...\n", task_id=task_id)
    append_console_output(f"已发起模型请求 model={model}", logger, task_id=task_id)
    stream = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages,
        stream=True,
    )
    stream_holder["stream"] = stream

    try:
        for event in stream:
            if task_id is not None and not is_task_active(task_id):
                try:
                    stream.close()
                except Exception:
                    pass
                raise TaskReplacedError("检测到新的上传请求，模型流已中止")

            delta = None
            try:
                delta = event.choices[0].delta.content
            except Exception:
                pass

            if delta:
                chunks.append(delta)
                append_partial_output(delta, task_id=task_id)
                if len(chunks) == 1:
                    append_console_output("已收到首个模型输出片段", logger, task_id=task_id)
    finally:
        stop_event.set()

    ensure_task_active(task_id)
    append_partial_output("\n[系统] 模型输出完成，正在整理结果...\n", task_id=task_id)
    append_console_output("模型输出结束，开始解析 JSON", logger, task_id=task_id)
    return "".join(chunks).strip()


def build_text_pdf_messages(pdf_text):
    return [
        {
            "role": "system",
            "content": (
                "你是结构化提取引擎。输出必须是 JSON 数组，"
                f"每个元素必须且只能包含这些字段：{', '.join(EXCEL_COLUMNS)}。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请从下面的英语单词表文本中提取结构化结果，并严格只返回 JSON 数组。\n"
                "禁止输出解释、markdown 或代码块。\n\n"
                f"原始文本如下：\n{pdf_text}"
            ),
        },
    ]


def build_image_pdf_prompt():
    return (
        "请识别这些英语单词表页面，并严格返回 JSON 数组。\n"
        f"每个元素必须且只能包含这些字段：{', '.join(EXCEL_COLUMNS)}。\n"
        "禁止输出解释、markdown 或代码块，只输出 JSON。"
    )


def extract_responses_text(response):
    """Extract text from OpenAI Responses API objects across SDK versions."""
    if getattr(response, "output_text", None):
        return response.output_text

    data = response.model_dump() if hasattr(response, "model_dump") else response
    outputs = data.get("output", []) if isinstance(data, dict) else []
    chunks = []
    for item in outputs:
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                chunks.append(content.get("text", ""))
    return "\n".join([chunk for chunk in chunks if chunk])


def convert_pdf_to_data_urls(pdf_bytes, poppler_path):
    try:
        images = convert_from_bytes(pdf_bytes, dpi=180, fmt="jpeg", poppler_path=poppler_path)
    except PDFInfoNotInstalledError:
        current_env = (os.getenv("POPPLER_PATH") or "").strip() or "<未设置>"
        current_which = shutil.which("pdfinfo") or "<未找到>"
        raise ValueError(
            "未检测到 Poppler。请安装 Poppler 并配置 PATH，或设置 POPPLER_PATH 指向 bin 目录。"
            f" 当前 POPPLER_PATH={current_env}，which(pdfinfo)={current_which}"
        )

    max_pages = min(len(images), int(os.getenv("PDF_MAX_PAGES", "8")))
    data_urls = []
    for image in images[:max_pages]:
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        data_urls.append(f"data:image/jpeg;base64,{encoded}")
    return data_urls


def extract_pdf_text(pdf_bytes, poppler_path):
    pdftotext_cmd = "pdftotext.exe" if os.name == "nt" else "pdftotext"
    if poppler_path:
        pdftotext_cmd = os.path.join(poppler_path, pdftotext_cmd)

    pdf_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
            tmp_pdf.write(pdf_bytes)
            pdf_path = tmp_pdf.name

        result = subprocess.run(
            [pdftotext_cmd, "-layout", "-enc", "UTF-8", pdf_path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "pdftotext 执行失败")
        return (result.stdout or "").strip()
    except FileNotFoundError:
        return ""
    finally:
        if pdf_path and os.path.exists(pdf_path):
            os.remove(pdf_path)


def find_poppler_path():
    env_path = (os.getenv("POPPLER_PATH") or "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    pdfinfo_in_path = shutil.which("pdfinfo")
    if pdfinfo_in_path:
        return os.path.dirname(pdfinfo_in_path)

    if os.name != "nt":
        return None

    candidates = [
        r"D:\poper\poppler-25.12.0\Library\bin",
        r"D:\poppler\poppler-25.12.0\Library\bin",
        r"D:\poppler\Library\bin",
        r"C:\Program Files\poppler\Library\bin",
        r"C:\Program Files (x86)\poppler\Library\bin",
        r"C:\poppler\Library\bin",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def strip_code_fence(text):
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def validate_rows_schema(rows):
    if not isinstance(rows, list):
        raise ValueError("AI 返回内容不是列表")

    expected_keys = set(EXCEL_COLUMNS)
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("AI 返回的列表元素不是对象")
        if set(item.keys()) != expected_keys:
            raise ValueError(f"AI 返回字段不符合要求，应为：{', '.join(EXCEL_COLUMNS)}")
