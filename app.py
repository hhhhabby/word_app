import os
import time
import json
import shutil
import tempfile
import subprocess
import base64
import threading
import logging
from collections import deque
import pandas as pd
import requests
from flask import Flask, render_template, request, send_file, jsonify
from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from openai import OpenAI
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError




app = Flask(__name__)
# 限制上传大小为 16MB
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

# 控制台日志缓冲区（用于前端实时查看）
console_log_buffer = deque(maxlen=400)


class InMemoryLogHandler(logging.Handler):
    """把后端控制台日志同步写入内存，供前端轮询查看。"""

    def emit(self, record):
        try:
            message = self.format(record)
            console_log_buffer.append(message)
        except Exception:
            pass


def init_log_capture():
    """初始化日志捕获，包含 werkzeug 请求日志。"""
    handler = InMemoryLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    for logger_name in ("werkzeug", app.logger.name):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(h, InMemoryLogHandler) for h in logger.handlers):
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def get_console_output_text():
    """读取最新控制台日志文本。"""
    return "\n".join(console_log_buffer)


init_log_capture()

# 进度数据存储
progress_data = {
    'total': 0,
    'processed': 0,
    'remaining': 0,
    'start_time': None,
    'end_time': None,
    'is_processing': False,
    'partial_output': '',
    'console_output': ''
}


def update_progress(stage, processed, total, is_processing=True):
    """统一更新进度，供前端轮询展示。"""
    global progress_data
    progress_data['stage'] = stage
    progress_data['total'] = total
    progress_data['processed'] = processed
    progress_data['remaining'] = max(total - processed, 0)
    progress_data['is_processing'] = is_processing
    if is_processing and not progress_data.get('start_time'):
        progress_data['start_time'] = datetime.now().isoformat()
    if not is_processing:
        progress_data['end_time'] = datetime.now().isoformat()


def append_console_output(message):
    """记录可实时展示给前端的后端日志。"""
    app.logger.info(message)
    progress_data['console_output'] = get_console_output_text()


def update_partial_output(text):
    """更新流式输出内容，避免前端拉取过大字符串。"""
    global progress_data
    max_len = 5000
    progress_data['partial_output'] = (text or '')[-max_len:]


def append_partial_output(text):
    """增量追加流式内容（用于心跳和 token 片段）。"""
    global progress_data
    max_len = 5000
    existing = progress_data.get('partial_output', '')
    progress_data['partial_output'] = (existing + (text or ''))[-max_len:]


def stream_chat_completion_text(client, model, messages, temperature=0):
    """流式获取大模型输出文本，并持续写入进度信息。"""
    chunks = []
    started_at = time.time()
    stop_event = threading.Event()

    def heartbeat():
        while not stop_event.wait(1.0):
            elapsed = int(time.time() - started_at)
            append_partial_output(f"\n[系统] 正在调用模型，已等待 {elapsed} 秒...")
            append_console_output(f"模型处理中，已等待 {elapsed} 秒")

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    append_partial_output("[系统] 已发起流式请求，等待模型返回内容...\n")
    append_console_output(f"已发起流式请求 model={model}")
    stream = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages,
        stream=True
    )

    try:
        for event in stream:
            try:
                delta = event.choices[0].delta.content
            except Exception:
                delta = None

            if delta:
                chunks.append(delta)
                append_partial_output(delta)
                if len(chunks) == 1:
                    append_console_output("已收到首个流式输出分片")
    finally:
        stop_event.set()

    append_partial_output("\n[系统] 模型输出完成，正在整理结果...\n")
    append_console_output("模型流式输出结束，开始解析 JSON")
    return ''.join(chunks).strip()


def validate_deepseek_key(api_key):
    """调用 DeepSeek 做轻量验证。"""
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        temperature=0,
        max_tokens=8,
        messages=[
            {"role": "system", "content": "你是健康检查助手，只回答OK。"},
            {"role": "user", "content": "请返回OK"}
        ]
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
            response = client.responses.create(
                model=model_id,
                input="请仅返回OK"
            )
            content = extract_responses_text(response).strip()
        else:
            response = client.chat.completions.create(
                model=model_id,
                temperature=0,
                messages=[{"role": "user", "content": "请仅返回OK"}]
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


def parse_pdf_with_deepseek(pdf_bytes, api_key):
    """提取 PDF 文本后交给 DeepSeek，返回严格 JSON 列表。"""
    poppler_path = find_poppler_path()
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

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    messages = [
        {
            "role": "system",
            "content": (
                "你是结构化提取引擎。输出必须是 JSON 数组，且每个元素仅包含"
                " Unit、英文单词/短语、词性、中文释义 四个字段。"
            )
        },
        {
            "role": "user",
            "content": (
                "请从下面的英语单词表文本中提取结构化结果，并严格只返回 JSON 数组。\n"
                "要求：每个元素必须且只包含 Unit、英文单词/短语、词性、中文释义。\n"
                "禁止输出解释、禁止 markdown、禁止代码块。\n\n"
                f"原始文本如下：\n{pdf_text}"
            )
        }
    ]
    content = stream_chat_completion_text(
        client,
        os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        messages,
        temperature=0
    )
    rows = json.loads(content)
    if not isinstance(rows, list):
        raise ValueError("AI返回内容不是列表")

    expected_keys = {"Unit", "英文单词/短语", "词性", "中文释义"}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("AI返回的列表元素不是对象")
        if set(item.keys()) != expected_keys:
            raise ValueError("AI返回字段不符合要求，请重试")

    return rows


def parse_pdf_with_doubao(pdf_bytes, api_key):
    """将 PDF 页面转图后提交给豆包多模态模型。"""
    poppler_path = find_poppler_path()
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
            )
        }
    ]

    for url in image_urls:
        content_items.append({"type": "input_image", "image_url": url})

    model_id = os.getenv("DOUBAO_MODEL", "doubao-seed-2-0-lite-260215")
    client = OpenAI(api_key=api_key, base_url="https://ark.cn-beijing.volces.com/api/v3")
    try:
        # 统一走 chat.completions 流式输出，前端可实时展示模型片段
        chat_content = []
        for item in content_items:
            if item.get("type") == "input_text":
                chat_content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "input_image":
                chat_content.append({"type": "image_url", "image_url": {"url": item.get("image_url", "")}})

        content = stream_chat_completion_text(
            client,
            model_id,
            [{"role": "user", "content": chat_content}],
            temperature=0
        )
    except Exception as e:
        raise ValueError(f"豆包调用失败（model={model_id}）：{str(e)}")

    rows = json.loads(content)
    if not isinstance(rows, list):
        raise ValueError("AI返回内容不是列表")

    expected_keys = {"Unit", "英文单词/短语", "词性", "中文释义"}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("AI返回的列表元素不是对象")
        if set(item.keys()) != expected_keys:
            raise ValueError("AI返回字段不符合要求，请重试")
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
    pdftotext_cmd = "pdftotext.exe" if os.name == 'nt' else "pdftotext"
    if poppler_path:
        pdftotext_cmd = os.path.join(poppler_path, pdftotext_cmd)

    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_pdf:
            tmp_pdf.write(pdf_bytes)
            pdf_path = tmp_pdf.name

        result = subprocess.run(
            [pdftotext_cmd, "-layout", "-enc", "UTF-8", pdf_path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "pdftotext 执行失败")
        return (result.stdout or "").strip()
    except FileNotFoundError:
        return ""
    finally:
        try:
            if 'pdf_path' in locals() and os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception:
            pass


def find_poppler_path():
    """优先使用环境变量，其次在 Windows 常见目录自动探测 Poppler。"""
    env_path = (os.getenv("POPPLER_PATH") or "").strip()
    if env_path and os.path.exists(env_path):
        return env_path

    # 如果 PATH 中可找到 pdfinfo，则直接使用其所在目录
    pdfinfo_in_path = shutil.which("pdfinfo")
    if pdfinfo_in_path:
        return os.path.dirname(pdfinfo_in_path)

    if os.name != 'nt':
        return None

    candidates = [
        r"D:\\poper\\poppler-25.12.0\\Library\\bin",
        r"D:\\poppler\\poppler-25.12.0\\Library\\bin",
        r"D:\\poppler\\Library\\bin",
        r"C:\\Program Files\\poppler\\Library\\bin",
        r"C:\\Program Files (x86)\\poppler\\Library\\bin",
        r"C:\\poppler\\Library\\bin"
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    return None

# --- 把你原来的函数搬过来，稍微修改一下 ---
def get_quji_help(word):
    """从趣记单词的 API 获取单个单词的谐音助记"""
    try:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        url = f"https://qujidanci.xieyonglin.com/api/word/lookup.php?word={word}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        response = session.get(url, headers=headers, timeout=15, verify=False)
        response.raise_for_status()
        json_data = response.json()
        
        memory_tips = json_data.get("data", {}).get("memory_tips", [])
        results = []
        for tip in memory_tips:
            method = tip.get("method", "")
            details = tip.get("details", "")
            if "谐音" in method:
                results.append(details)

        if results:
            return "；".join(results)
        return "未找到谐音助记"
    except Exception as e:
        return f"查询失败：{str(e)[:30]}"

# --- Web 路由 ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/progress')
def get_progress():
    progress_data['console_output'] = get_console_output_text()
    return jsonify(progress_data)


@app.route('/api/validate-key', methods=['POST'])
def api_validate_key():
    """验证用户输入的 DeepSeek API Key 是否可用。"""
    data = request.get_json(silent=True) or {}
    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        return jsonify({'ok': False, 'message': '请先输入API密钥'}), 400

    try:
        provider = infer_provider(api_key)
        if provider == "doubao":
            validate_doubao_key(api_key)
        else:
            validate_deepseek_key(api_key)
        return jsonify({'ok': True, 'message': '密钥有效'})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'密钥无效：{str(e)}'}), 400


@app.route('/api/process-pdf', methods=['POST'])
def api_process_pdf():
    """处理 PDF，调用 DeepSeek 识别并导出 Excel。"""
    global progress_data

    if 'file' not in request.files:
        return 'PDF上传失败：未检测到文件', 400

    api_key = (request.form.get('api_key') or '').strip()
    if not api_key:
        return 'API密钥无效，请检查后重新输入', 400

    file = request.files['file']
    if not file or file.filename == '':
        return 'PDF上传失败：未选择文件', 400

    if not file.filename.lower().endswith('.pdf'):
        return '仅支持PDF文件', 400

    progress_data = {
        'total': 4,
        'processed': 0,
        'remaining': 4,
        'start_time': datetime.now().isoformat(),
        'end_time': None,
        'is_processing': True,
        'stage': '准备开始',
        'partial_output': '',
        'console_output': ''
    }
    append_console_output("任务开始：已接收上传文件")

    try:
        update_progress('正在读取PDF', 1, 4, True)
        append_console_output("阶段1/4：正在读取PDF")
        pdf_bytes = file.read()
        if not pdf_bytes:
            raise ValueError('PDF文件损坏，请重新上传')
        append_console_output(f"PDF读取完成，大小 {len(pdf_bytes)} 字节")

        update_progress('正在调用AI识别', 2, 4, True)
        append_console_output("阶段2/4：正在调用AI识别")
        provider = infer_provider(api_key)
        append_console_output(f"已识别供应商：{provider}")
        if provider == "doubao":
            rows = parse_pdf_with_doubao(pdf_bytes, api_key)
        else:
            rows = parse_pdf_with_deepseek(pdf_bytes, api_key)
        append_console_output(f"AI识别完成，返回 {len(rows)} 条记录")

        update_progress('正在生成Excel', 3, 4, True)
        append_console_output("阶段3/4：正在生成Excel")
        df = pd.DataFrame(rows)
        output = BytesIO()
        df.to_excel(output, index=False, engine='openpyxl')
        output.seek(0)
        append_console_output("Excel内存文件生成完成")

        update_progress('导出完成', 4, 4, False)
        append_console_output("阶段4/4：导出完成，开始返回下载")
        return send_file(
            output,
            as_attachment=True,
            download_name='英语单词表_AI识别版.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except json.JSONDecodeError:
        progress_data['is_processing'] = False
        progress_data['end_time'] = datetime.now().isoformat()
        append_console_output("错误：AI返回格式异常，JSON解析失败")
        return 'AI返回格式异常，无法解析为JSON，请重试', 500
    except Exception as e:
        progress_data['is_processing'] = False
        progress_data['end_time'] = datetime.now().isoformat()
        append_console_output(f"错误：{str(e)}")
        return f'处理失败：{str(e)}', 500

@app.route('/process', methods=['POST'])
def process_file():
    global progress_data
    
    if 'file' not in request.files:
        return "没有上传文件", 400
    
    file = request.files['file']
    if file.filename == '':
        return "未选择文件", 400

    # 重置进度
    progress_data = {
        'total': 0,
        'processed': 0,
        'remaining': 0,
        'start_time': datetime.now().isoformat(),
        'end_time': None,
        'is_processing': True
    }

    try:
        # 1. 读取上传的 Excel (内存中处理)
        df = pd.read_excel(file)
        
        # 假设列名还是 "英文"
        if "英文" not in df.columns:
            return "Excel 中必须包含'英文'列", 400
            
        words = df["英文"].dropna().tolist()
        
        # 更新进度总数
        progress_data['total'] = len(words)
        progress_data['remaining'] = len(words)
        
        # 2. 批量查询
        mnemonics = []
        for i, word in enumerate(words, 1):
            help_text = get_quji_help(word)
            mnemonics.append(help_text)
            
            # 更新进度
            progress_data['processed'] = i
            progress_data['remaining'] = len(words) - i
            
            time.sleep(2)  # 保持间隔
            
        # 3. 写回 DataFrame
        df["趣记单词_谐音助记"] = mnemonics
        
        # 完成
        progress_data['end_time'] = datetime.now().isoformat()
        progress_data['is_processing'] = False
        
        # 4. 保存到内存流并返回下载
        output = BytesIO()
        df.to_excel(output, index=False, engine='openpyxl')
        output.seek(0)
        
        return send_file(
            output,
            as_attachment=True,
            download_name=f'带谐音助记_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        progress_data['is_processing'] = False
        return f"处理出错：{str(e)}", 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5001)