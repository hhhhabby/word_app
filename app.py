from flask import Flask

from progress_store import init_log_capture
from routes.excel_api import excel_api_bp
from routes.pages import pages_bp
from routes.pdf_api import pdf_api_bp


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

    init_log_capture(app)

    app.register_blueprint(pages_bp)
    app.register_blueprint(pdf_api_bp)
    app.register_blueprint(excel_api_bp)
    return app


app = create_app()


if __name__ == "__main__":
    # 允许处理请求时并发响应进度轮询接口
    app.run(debug=False, host="0.0.0.0", port=5001, threaded=True)
