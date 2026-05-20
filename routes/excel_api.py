import time
from datetime import datetime
from io import BytesIO

from flask import Blueprint, request, send_file
import pandas as pd

from progress_store import mark_failed, reset_progress, update_progress
from services.quji_service import get_quji_help


excel_api_bp = Blueprint("excel_api", __name__)
WORD_COLUMN = "英文"
MNEMONIC_COLUMN = "趣记单词_谐音助记"


@excel_api_bp.route("/api/process-excel", methods=["POST"])
@excel_api_bp.route("/process", methods=["POST"])
def process_excel_file():
    if "file" not in request.files:
        return "没有上传文件", 400

    file = request.files["file"]
    if file.filename == "":
        return "未选择文件", 400

    task_id = reset_progress(stage="正在读取 Excel", is_processing=True)

    try:
        df = pd.read_excel(file)
        if WORD_COLUMN not in df.columns:
            mark_failed("缺少英文列", task_id=task_id)
            return f"Excel 中必须包含“{WORD_COLUMN}”列", 400

        word_mask = df[WORD_COLUMN].notna()
        words = [str(word).strip() for word in df.loc[word_mask, WORD_COLUMN].tolist()]
        total = len(words)
        update_progress("正在查询谐音助记", 0, total, True, task_id=task_id)

        mnemonics = []
        for index, word in enumerate(words, 1):
            mnemonics.append(get_quji_help(word))
            update_progress(f"正在处理：{word}", index, total, True, task_id=task_id)
            time.sleep(2)

        df.loc[word_mask, MNEMONIC_COLUMN] = mnemonics
        update_progress("正在生成 Excel", total, total, True, task_id=task_id)

        output = BytesIO()
        df.to_excel(output, index=False, engine="openpyxl")
        output.seek(0)

        update_progress("处理完成", total, total, False, task_id=task_id)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"带谐音助记_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        mark_failed("处理失败", task_id=task_id)
        return f"处理出错：{exc}", 500
