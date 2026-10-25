from flask import Blueprint, render_template, request, flash

import logic

bp = Blueprint("feature", __name__)


def _read_input(file_field, text_field, label):
    """Return (text, warnings) from an uploaded file or pasted text."""
    f = request.files.get(file_field)
    if f and f.filename:
        raw = f.read(logic.MAX_BYTES + 1)
        return logic.decode_bytes(raw, label)
    pasted = (request.form.get(text_field) or "").strip()
    if pasted:
        return logic.decode_bytes(pasted.encode("utf-8"), label)
    raise logic.InputError(f"Please upload or paste your {label} CSV (or click \"Try with sample data\").")


@bp.route("/", methods=["GET", "POST"])
def index():
    result = None
    is_demo = False
    if request.method == "POST":
        try:
            warnings = []
            if request.form.get("demo"):
                is_demo = True
                s_text, p_text = logic.demo_data()
            else:
                s_text, w1 = _read_input("signups_file", "signups_text", "signups")
                p_text, w2 = _read_input("payments_file", "payments_text", "payments")
                warnings += w1 + w2
            result = logic.analyze(s_text, p_text)
            for w in warnings + result["warnings"]:
                flash(w, "warning")
        except logic.InputError as e:
            flash(str(e), "error")
        except Exception:
            flash("Something went wrong while reading your files. Please check that both are valid CSV exports.", "error")
    return render_template("index.html", result=result, is_demo=is_demo)
