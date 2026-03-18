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


def stream_chat_completion_text(client, model, messages, logger, temperature=0, task_id=None):
    """流式获取大模型输出文本，并持续写入进度信息。"""
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
            append_partial_output(f"\n[系统] 正在调用模型，已等待 {elapsed} 秒...", task_id=task_id)
            append_console_output(f"模型处理中，已等待 {elapsed} 秒", logger, task_id=task_id)

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    ensure_task_active(task_id)
    update_partial_output("[系统] 已发起流式请求，等待模型返回内容...\n", task_id=task_id)
    append_console_output(f"已发起流式请求 model={model}", logger, task_id=task_id)
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

            try:
                delta = event.choices[0].delta.content
            except Exception:
                delta = None

            if delta:
                chunks.append(delta)
                append_partial_output(delta, task_id=task_id)
                if len(chunks) == 1:
                    append_console_output("已收到首个流式输出分片", logger, task_id=task_id)
    finally:
        stop_event.set()

    ensure_task_active(task_id)
    append_partial_output("\n[系统] 模型输出完成，正在整理结果...\n", task_id=task_id)
    append_console_output("模型流式输出结束，开始解析 JSON", logger, task_id=task_id)
    return "".join(chunks).strip()


def extract_responses_text(response):
    """兼容不同 SDK 版本，从 responses.create 返回中提取文本。"""
    if getattr(response, "output_text", None):
        return response.output_text

    data = response.model_dump() if hasattr(response, "model_dump") else response
    outputs = data.get("output", []) if isinstance(data, dict) else []
    chunks = []
    for item in outputs:
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text"):
                chunks.append(content.get("text", ""))
    return "\n".join([x for x in chunks if x])


def validate_deepseek_key(api_key):
    """调用 DeepSeek 做轻量验证。"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        temperature=0,
        max_tokens=8,
        messages=[
            {"role": "system", "content": "你是健康检查助手，只回答OK。"},
            {"role": "user", "content": "请返回OK"},
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    if "OK" not in content.upper():
        raise ValueError("密钥验证失败")


def validate_doubao_key(api_key):
    """调用豆包(方舟)做轻量验证。"""
    model_id = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-lite-260215")
    client = OpenAI(api_key=api_key, base_url="https://ark.cn-beijing.volces.com/api/v3")
    try:
        if hasattr(client, "responses"):
            response = client.responses.create(model=model_id, input="请仅返回OK")
            content = extract_responses_text(response).strip()
        else:
            response = client.chat.completions.create(
                model=model_id,
                temperature=0,
                messages=[{"role": "user", "content": "请仅返回OK"}],
            )
            content = (response.choices[0].message.content or "").strip()
        if "OK" not in content.upper():
            raise ValueError("密钥验证失败")
    except Exception as e:
        raise ValueError(f"方舟校验失败（model={model_id}）：{str(e)}")


def infer_provider(api_key):
    """自动识别供应商：sk- 默认 DeepSeek，其余默认豆包方舟。"""
    key = (api_key or "").strip().lower()
    if key.startswith("sk-"):
        return "deepseek"
    return "doubao"


def parse_pdf_with_deepseek(pdf_bytes, api_key, logger, task_id=None):
    """提取 PDF 文本后交给 DeepSeek，返回严格 JSON 列表。"""
    poppler_path = find_poppler_path()
    ensure_task_active(task_id)
    pdf_text = extract_pdf_text(pdf_bytes, poppler_path)
    if not pdf_text:
        current_env = (os.getenv("POPPLER_PATH") or "").strip() or "<未设置>"
        current_which = shutil.which("pdfinfo") or "<未找到>"
        raise ValueError(
            "PDF 未提取到可识别文本。请确认 Poppler 可用，且 PDF 不是纯图片扫描件。"
            f" 当前POPPLER_PATH={current_env}；which(pdfinfo)={current_which}"
        )

    max_chars = int(os.getenv("PDF_MAX_TEXT_CHARS", "120000"))
    pdf_text = pdf_text[:max_chars]
    ensure_task_active(task_id)

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    messages = [
        {
            "role": "system",
            "content": (
                "你是结构化提取引擎。输出必须是 JSON 数组，且每个元素仅包含"
                " Unit、英文单词/短语、词性、中文释义 四个字段。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请从下面的英语单词表文本中提取结构化结果，并严格只返回 JSON 数组。\n"
                "要求：每个元素必须且只包含 Unit、英文单词/短语、词性、中文释义。\n"
                "禁止输出解释、禁止 markdown、禁止代码块。\n\n"
                f"原始文本如下：\n{pdf_text}"
            ),
        },
    ]
    content = stream_chat_completion_text(
        client,
        os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        messages,
        logger,
        temperature=0,
        task_id=task_id,
    )
    ensure_task_active(task_id)
    rows = json.loads(content)
    _validate_rows_schema(rows)
    return rows


def parse_pdf_with_doubao(pdf_bytes, api_key, logger, task_id=None):
    """将 PDF 页面转图后提交给豆包多模态模型。"""
    poppler_path = find_poppler_path()
    ensure_task_active(task_id)
    image_urls = convert_pdf_to_data_urls(pdf_bytes, poppler_path)
    if not image_urls:
        raise ValueError("PDF 转图片失败，无法提交豆包多模态识别")

    content_items = [
        {
            "type": "input_text",
            "text": (
                "请识别这些英语单词表页面，并严格返回 JSON 数组。\n"
                "每个元素必须且只包含以下4个键：Unit、英文单词/短语、词性、中文释义。\n"
                "禁止输出解释、禁止 markdown、禁止代码块，只输出 JSON。"
            ),
        }
    ]

    for url in image_urls:
        content_items.append({"type": "input_image", "image_url": url})

    model_id = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-lite-260215")
    client = OpenAI(api_key=api_key, base_url="https://ark.cn-beijing.volces.com/api/v3")
    try:
        ensure_task_active(task_id)
        chat_content = []
        for item in content_items:
            if item.get("type") == "input_text":
                chat_content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "input_image":
                chat_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": item.get("image_url", "")},
                    }
                )

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
    except Exception as e:
        raise ValueError(f"豆包调用失败（model={model_id}）：{str(e)}")

    ensure_task_active(task_id)
    rows = json.loads(content)
    _validate_rows_schema(rows)
    return rows


def convert_pdf_to_data_urls(pdf_bytes, poppler_path):
    """将 PDF 转为若干 JPEG data URL，供多模态接口使用。"""
    try:
        images = convert_from_bytes(pdf_bytes, dpi=180, fmt="jpeg", poppler_path=poppler_path)
    except PDFInfoNotInstalledError:
        current_env = (os.getenv("POPPLER_PATH") or "").strip() or "<未设置>"
        current_which = shutil.which("pdfinfo") or "<未找到>"
        raise ValueError(
            "未检测到 Poppler。请安装 Poppler 并配置 PATH，或设置 POPPLER_PATH 指向 poppler 的 bin 目录。"
            f" 当前POPPLER_PATH={current_env}；which(pdfinfo)={current_which}"
        )

    max_pages = min(len(images), int(os.getenv("PDF_MAX_PAGES", "8")))
    data_urls = []
    for img in images[:max_pages]:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        data_urls.append(f"data:image/jpeg;base64,{b64}")
    return data_urls


def extract_pdf_text(pdf_bytes, poppler_path):
    """使用 Poppler 的 pdftotext 将 PDF 转为纯文本。"""
    pdftotext_cmd = "pdftotext.exe" if os.name == "nt" else "pdftotext"
    if poppler_path:
        pdftotext_cmd = os.path.join(poppler_path, pdftotext_cmd)

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
        try:
            if "pdf_path" in locals() and os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception:
            pass


def find_poppler_path():
    """优先使用环境变量，其次在 Windows 常见目录自动探测 Poppler。"""
    env_path = (os.getenv("POPPLER_PATH") or "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    pdfinfo_in_path = shutil.which("pdfinfo")
    if pdfinfo_in_path:
        return os.path.dirname(pdfinfo_in_path)

    if os.name != "nt":
        return None

    candidates = [
        r"D:\\poper\\poppler-25.12.0\\Library\\bin",
        r"D:\\poppler\\poppler-25.12.0\\Library\\bin",
        r"D:\\poppler\\Library\\bin",
        r"C:\\Program Files\\poppler\\Library\\bin",
        r"C:\\Program Files (x86)\\poppler\\Library\\bin",
        r"C:\\poppler\\Library\\bin",
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    return None


def _validate_rows_schema(rows):
    if not isinstance(rows, list):
        raise ValueError("AI返回内容不是列表")

    expected_keys = {"Unit", "英文单词/短语", "词性", "中文释义"}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("AI返回的列表元素不是对象")
        if set(item.keys()) != expected_keys:
            raise ValueError("AI返回字段不符合要求，请重试")
