import json
from io import BytesIO

import pandas as pd
from flask import Blueprint, current_app, jsonify, request, send_file

from progress_store import (
    TaskReplacedError,
    append_console_output,
    ensure_task_active,
    get_progress_payload,
    mark_failed,
    reset_progress,
    update_progress,
)
from services.pdf_service import (
    infer_provider,
    parse_pdf_with_deepseek,
    parse_pdf_with_doubao,
    validate_deepseek_key,
    validate_doubao_key,
)


pdf_api_bp = Blueprint("pdf_api", __name__)


@pdf_api_bp.route("/api/progress")
def get_progress():
    return jsonify(get_progress_payload())


@pdf_api_bp.route("/api/validate-key", methods=["POST"])
def api_validate_key():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "message": "请先输入 API 密钥"}), 400

    try:
        provider = infer_provider(api_key)
        if provider == "doubao":
            validate_doubao_key(api_key)
        else:
            validate_deepseek_key(api_key)
        return jsonify({"ok": True, "message": "密钥有效", "provider": provider})
    except Exception as exc:
        return jsonify({"ok": False, "message": f"密钥无效：{exc}"}), 400


@pdf_api_bp.route("/api/process-pdf", methods=["POST"])
def api_process_pdf():
    if "file" not in request.files:
        return "PDF 上传失败：未检测到文件", 400

    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return "API 密钥不能为空", 400

    file = request.files["file"]
    if not file or file.filename == "":
        return "PDF 上传失败：未选择文件", 400

    if not file.filename.lower().endswith(".pdf"):
        return "仅支持 PDF 文件", 400

    task_id = reset_progress(total=4, stage="准备开始", is_processing=True)
    append_console_output("任务开始：已接收上传文件", current_app.logger, task_id=task_id)

    try:
        ensure_task_active(task_id)
        update_progress("正在读取 PDF", 1, 4, True, task_id=task_id)
        pdf_bytes = file.read()
        if not pdf_bytes:
            raise ValueError("PDF 文件为空或已损坏，请重新上传")
        append_console_output(f"PDF 读取完成，大小 {len(pdf_bytes)} 字节", current_app.logger, task_id=task_id)

        ensure_task_active(task_id)
        update_progress("正在调用 AI 识别", 2, 4, True, task_id=task_id)
        provider = infer_provider(api_key)
        append_console_output(f"已识别供应商：{provider}", current_app.logger, task_id=task_id)
        if provider == "doubao":
            rows = parse_pdf_with_doubao(pdf_bytes, api_key, current_app.logger, task_id=task_id)
        else:
            rows = parse_pdf_with_deepseek(pdf_bytes, api_key, current_app.logger, task_id=task_id)
        append_console_output(f"AI 识别完成，返回 {len(rows)} 条记录", current_app.logger, task_id=task_id)

        ensure_task_active(task_id)
        update_progress("正在生成 Excel", 3, 4, True, task_id=task_id)
        output = BytesIO()
        pd.DataFrame(rows).to_excel(output, index=False, engine="openpyxl")
        output.seek(0)

        ensure_task_active(task_id)
        update_progress("导出完成", 4, 4, False, task_id=task_id)
        append_console_output("Excel 生成完成，开始返回下载", current_app.logger, task_id=task_id)
        return send_file(
            output,
            as_attachment=True,
            download_name="英语单词表_AI识别版.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except TaskReplacedError:
        append_console_output("旧任务已中止：检测到新的上传请求", current_app.logger, task_id=task_id)
        return "任务已被新的上传请求替代，请以最新任务结果为准", 409
    except json.JSONDecodeError:
        mark_failed("AI 返回格式异常", task_id=task_id)
        append_console_output("错误：AI 返回格式异常，JSON 解析失败", current_app.logger, task_id=task_id)
        return "AI 返回格式异常，无法解析为 JSON，请重试", 500
    except Exception as exc:
        mark_failed("处理失败", task_id=task_id)
        append_console_output(f"错误：{exc}", current_app.logger, task_id=task_id)
        return f"处理失败：{exc}", 500
