import os
import uuid
import threading
import time
import logging
import subprocess
import shutil
from flask import request, jsonify
from werkzeug.utils import secure_filename
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Detect Tesseract location
# ------------------------------------------------------------
TESSERACT_AVAILABLE = False
TESSERACT_PATH = None

# Try to find tesseract binary
possible_paths = [
    shutil.which('tesseract'),
    '/usr/bin/tesseract',
    '/usr/local/bin/tesseract',
    '/opt/homebrew/bin/tesseract',  # macOS
]

for path in possible_paths:
    if path and os.path.exists(path):
        try:
            # Test if it runs
            subprocess.run([path, '--version'], capture_output=True, check=True)
            TESSERACT_PATH = path
            TESSERACT_AVAILABLE = True
            logger.info(f"Tesseract found at: {path}")
            break
        except:
            continue

if not TESSERACT_AVAILABLE:
    logger.warning("Tesseract not found. OCR will not work. Install tesseract-ocr and ensure it's in PATH.")

# Set the path for pytesseract if found
if TESSERACT_AVAILABLE:
    try:
        import pytesseract
        from PIL import Image
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
        logger.info("Tesseract path set in pytesseract")
    except ImportError as e:
        logger.error(f"Failed to import pytesseract or PIL: {e}")
        TESSERACT_AVAILABLE = False

# Check for poppler-utils (for PDFs)
POPPLER_AVAILABLE = False
try:
    subprocess.run(['pdftoppm', '-v'], capture_output=True, check=True)
    from pdf2image import convert_from_path
    POPPLER_AVAILABLE = True
    logger.info("poppler-utils found, PDF support enabled")
except:
    logger.warning("poppler-utils not found. PDF processing will not work.")

# Supported languages (ISO 639-2 codes)
LANGUAGES = {
    'eng': 'English',
    'spa': 'Spanish',
    'fra': 'French',
    'deu': 'German',
    'ita': 'Italian',
    'por': 'Portuguese',
    'rus': 'Russian',
    'jpn': 'Japanese',
    'kor': 'Korean',
    'zho': 'Chinese (Simplified)',
    'ara': 'Arabic',
    'hin': 'Hindi',
    'tur': 'Turkish',
    'nld': 'Dutch',
    'pol': 'Polish',
    'swe': 'Swedish',
    'nor': 'Norwegian',
    'dan': 'Danish',
    'fin': 'Finnish',
    'heb': 'Hebrew',
}

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.pdf'}

def extract_text_from_image(image_path, lang='eng'):
    """Extract text from a single image using Tesseract."""
    if not TESSERACT_AVAILABLE:
        raise Exception("Tesseract is not installed or not found in PATH. Please install tesseract-ocr (sudo apt install tesseract-ocr) and restart the server.")
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(image_path)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        text = pytesseract.image_to_string(img, lang=lang)
        return text.strip()
    except Exception as e:
        logger.exception(f"OCR error on {image_path}")
        raise Exception(f"OCR failed: {str(e)}")

def extract_text_from_pdf(pdf_path, lang='eng'):
    """Extract text from a PDF by converting each page to an image and OCR'ing."""
    if not POPPLER_AVAILABLE:
        raise Exception("poppler-utils not installed. Cannot process PDFs. Install poppler-utils (sudo apt install poppler-utils).")
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=300)
        all_text = []
        for i, img in enumerate(images, 1):
            if img.mode != 'RGB':
                img = img.convert('RGB')
            text = pytesseract.image_to_string(img, lang=lang)
            all_text.append(f"--- Page {i} ---\n{text}\n")
        return '\n'.join(all_text)
    except Exception as e:
        logger.exception(f"PDF OCR error on {pdf_path}")
        raise Exception(f"PDF OCR failed: {str(e)}")

def process_ocr_task(task_id, input_path, original_filename, lang):
    task = load_task(task_id)
    if not task:
        return
    task['status'] = 'processing'
    task['progress'] = 0
    save_task(task_id, task)

    try:
        ext = os.path.splitext(original_filename)[1].lower()
        if ext == '.pdf':
            task['progress'] = 10
            save_task(task_id, task)
            text = extract_text_from_pdf(input_path, lang)
        else:
            task['progress'] = 20
            save_task(task_id, task)
            text = extract_text_from_image(input_path, lang)

        task['progress'] = 80
        save_task(task_id, task)

        base, _ = os.path.splitext(original_filename)
        output_filename = f"{base}_ocr.txt"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)
        counter = 1
        while os.path.exists(output_path):
            output_filename = f"{base}_ocr_{counter}.txt"
            output_path = os.path.join(UPLOAD_FOLDER, output_filename)
            counter += 1

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)

        os.remove(input_path)

        task = load_task(task_id)
        task['status'] = 'done'
        task['progress'] = 100
        task['output_file'] = output_filename
        save_task(task_id, task)
        logger.info(f"OCR completed for {task_id}, output: {output_filename}")

    except Exception as e:
        logger.exception(f"OCR task {task_id} failed")
        task = load_task(task_id)
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
            save_task(task_id, task)
        if os.path.exists(input_path):
            os.remove(input_path)

def register_routes(app):
    @app.route('/ocr/upload', methods=['POST'])
    def ocr_upload():
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': f'Unsupported file type. Allowed: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

        lang = request.form.get('language', 'eng')
        if lang not in LANGUAGES:
            return jsonify({'error': f'Unsupported language: {lang}'}), 400

        if not TESSERACT_AVAILABLE:
            return jsonify({'error': 'Tesseract is not installed. Please install tesseract-ocr (sudo apt install tesseract-ocr) and restart the server.'}), 500

        filename = secure_filename(file.filename)
        temp_path = os.path.join(UPLOAD_FOLDER, f"temp_{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        task_id = str(uuid.uuid4())
        task_data = {
            'task_id': task_id,
            'status': 'queued',
            'progress': 0,
            'created_at': time.time(),
            'cancelled': False,
            'type': 'ocr'
        }
        save_task(task_id, task_data)

        threading.Thread(target=process_ocr_task, args=(task_id, temp_path, filename, lang), daemon=True).start()

        return jsonify({'task_id': task_id})

    @app.route('/ocr/languages')
    def ocr_languages():
        return jsonify(LANGUAGES)
