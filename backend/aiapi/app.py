"""
AI Teacher Backend - Flask application entrypoint.
"""

import asyncio
import hashlib
import os
import json
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

from services.gemini_service import get_gemini_service
from services.pdf_service import extract_text_from_pdf

load_dotenv()

app = Flask(__name__)
CORS(app)


materials_store: dict[str, str] = {}
MATERIALS_DIR = Path(os.getenv("AI_MATERIALS_DIR", "/tmp/ai_materials"))
MATERIALS_DIR.mkdir(parents=True, exist_ok=True)


def parse_request_data() -> dict:
    """
    Merge JSON body, raw body (if JSON), form fields, and query params.
    Makes the endpoints tolerant to clients that send JSON with odd headers
    or fall back to form-urlencoded.
    """
    data = request.get_json(silent=True) or {}

    if not data and request.data:
        try:
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            data = {}


    if request.form:
        for k, v in request.form.items():
            data.setdefault(k, v)


    if request.args:
        for k, v in request.args.items():
            data.setdefault(k, v)

    return data


def run_async(coro):
    """Run async service methods from Flask sync handlers."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def resolve_material(data: dict) -> str:
    """Resolve material from material_id or inline text."""
    material_id = data.get("material_id")
    material = data.get("material")

    if material_id and material_id in materials_store:
        material = materials_store[material_id]


    if material_id and not material:
        path = MATERIALS_DIR / f"{material_id}.txt"
        if path.exists():
            try:
                material = path.read_text(encoding="utf-8")
                materials_store[material_id] = material
            except Exception:
                pass

    return material or ""


def normalize_lang(data: dict) -> str | None:
    """Accept both `language` and `lang` payload fields."""
    value = data.get("language", data.get("lang"))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "AI Teacher API is running"})


@app.route("/api/upload", methods=["POST"])
def upload_material():
    try:
        material_text = ""

        if "file" in request.files:
            file = request.files["file"]
            if not file.filename or not file.filename.lower().endswith(".pdf"):
                return jsonify({"error": "Only PDF files are supported"}), 400
            material_text = extract_text_from_pdf(file)
        elif "text" in request.form:
            material_text = request.form["text"]
        elif request.is_json:
            data = request.get_json(silent=True) or {}
            material_text = data.get("text", "")

        material_text = str(material_text or "").strip()
        if not material_text:
            return jsonify({"error": "Material not found"}), 400

        material_id = hashlib.md5(material_text[:100].encode("utf-8")).hexdigest()[:12]
        materials_store[material_id] = material_text
        try:
            path = MATERIALS_DIR / f"{material_id}.txt"
            path.write_text(material_text, encoding="utf-8")
        except Exception:
            pass

        return jsonify(
            {
                "material_id": material_id,
                "preview": material_text[:500] + ("..." if len(material_text) > 500 else ""),
                "length": len(material_text),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generate/learn", methods=["POST"])
def generate_learn():
    try:
        data = parse_request_data()
        material = resolve_material(data)
        history_mode = bool(data.get("history_mode", False))
        lang = normalize_lang(data)

        if not material:
            return jsonify({"error": "Material not found"}), 400

        gemini = get_gemini_service()
        result = run_async(gemini.generate_learn_content(material, history_mode, lang))
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generate/practice", methods=["POST"])
def generate_practice():
    try:
        data = parse_request_data()
        material = resolve_material(data)
        count = data.get("count", 10)
        exclude_questions = data.get("exclude_questions", [])
        lang = normalize_lang(data)

        if not material:
            return jsonify({"error": "Material not found"}), 400

        if count not in [10, 15, 20, 25, 30]:
            count = 10
        if not isinstance(exclude_questions, list):
            exclude_questions = []

        gemini = get_gemini_service()
        result = run_async(
            gemini.generate_practice_questions(material, count, exclude_questions, lang)
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generate/realtest", methods=["POST"])
def generate_realtest():
    try:
        data = parse_request_data()
        material = resolve_material(data)
        count = data.get("count", 10)
        lang = normalize_lang(data)

        if not material:
            return jsonify({"error": "Material not found"}), 400

        if count not in [10, 15, 20, 25, 30]:
            count = 10

        gemini = get_gemini_service()
        result = run_async(gemini.generate_realtest_questions(material, count, lang))
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generate/continue", methods=["POST"])
def generate_continue():
    try:
        data = parse_request_data()
        material = resolve_material(data)
        count = data.get("count", 10)
        previous_questions = data.get("previous_questions", [])
        lang = normalize_lang(data)

        if not material:
            return jsonify({"error": "Material not found"}), 400

        if count not in [10, 15, 20, 25, 30]:
            count = 10
        if not isinstance(previous_questions, list):
            previous_questions = []

        gemini = get_gemini_service()
        result = run_async(
            gemini.generate_practice_questions(material, count, previous_questions, lang)
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("AI_TEACHER_PORT", os.getenv("FLASK_PORT", "5000")))
    debug = os.getenv("FLASK_DEBUG", "true").strip().lower() == "true"

    print(f"AI Teacher API starting on port {port}")
    print("Ready to help with ENT preparation")

    app.run(host="0.0.0.0", port=port, debug=debug)
