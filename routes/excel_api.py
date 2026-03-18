import time
from datetime import datetime
from io import BytesIO

from flask import Blueprint, jsonify, request, send_file
import pandas as pd

from progress_store import progress_data
from services.quji_service import get_quji_help


excel_api_bp = Blueprint("excel_api", __name__)


@excel_api_bp.route("/api/process-excel", methods=["POST"])
@excel_api_bp.route("/process", methods=["POST"])
def process_excel_file():
    if "file" not in request.files:
        return "没有上传文件", 400

    file = request.files["file"]
    if file.filename == "":
        return "未选择文件", 400

    progress_data.clear()
    progress_data.update(
        {
            "total": 0,
            "processed": 0,
            "remaining": 0,
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "is_processing": True,
        }
    )

    try:
        df = pd.read_excel(file)
        if "英文" not in df.columns:
            return "Excel 中必须包含'英文'列", 400

        words = df["英文"].dropna().tolist()

        progress_data["total"] = len(words)
        progress_data["remaining"] = len(words)

        mnemonics = []
        for i, word in enumerate(words, 1):
            help_text = get_quji_help(word)
            mnemonics.append(help_text)
            progress_data["processed"] = i
            progress_data["remaining"] = len(words) - i
            time.sleep(2)

        df["趣记单词_谐音助记"] = mnemonics

        progress_data["end_time"] = datetime.now().isoformat()
        progress_data["is_processing"] = False

        output = BytesIO()
        df.to_excel(output, index=False, engine="openpyxl")
        output.seek(0)

        return send_file(
            output,
            as_attachment=True,
            download_name=f"带谐音助记_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        progress_data["is_processing"] = False
        return f"处理出错：{str(e)}", 500
