import os
import time
import pandas as pd
import requests
from flask import Flask, render_template, request, send_file, jsonify
from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime

app = Flask(__name__)
# 限制上传大小为 16MB
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

# 进度数据存储
progress_data = {
    'total': 0,
    'processed': 0,
    'remaining': 0,
    'start_time': None,
    'end_time': None,
    'is_processing': False
}

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
    return jsonify(progress_data)

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