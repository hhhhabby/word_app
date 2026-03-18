from flask import Blueprint, render_template


pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def index():
    return render_template("index.html")


@pages_bp.route("/pdf-to-excel")
def pdf_to_excel_page():
    return render_template("pdf_to_excel.html")


@pages_bp.route("/excel-mnemonic")
def excel_mnemonic_page():
    return render_template("excel_mnemonic.html")
