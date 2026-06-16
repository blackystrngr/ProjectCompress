import os
import uuid
import threading
import time
import logging
import subprocess
from flask import request, jsonify
from werkzeug.utils import secure_filename
from tasks import save_task, load_task
from config import UPLOAD_FOLDER

logger = logging.getLogger(__name__)

# Check for required external tools
TESSERACT_AVAILABLE = False
POPPLER_AVAILABLE = False

try:
    # Try to import pytesseract and check if tesseract is installed
    import pytesseract
    from PIL import Image
    # Run a simple version check
    subprocess.run(['tesseract', '--version'], capture_output=True, check=True)
    TESSERACT_AVAILABLE = True
except:
    logger.warning("Tesseract not found. OCR will not work. Install tesseract-ocr and python3-pytesseract.")

try:
    # Check for pdf2image and poppler-utils
    from pdf2image import convert_from_path
    # Try to run pdftoppm (part of poppler)
    subprocess.run(['pdftoppm', '-v'], capture_output=True, check=True)
    POPPLER_AVAILABLE = True
except:
    logger.warning("pdf2image or poppler-utils not found. PDF conversion will not work.")

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
        raise Exception("Tesseract is not installed. Please install tesseract-ocr.")
    try:
        img = Image.open(image_path)
        # Convert to RGB if needed (e.g., for RGBA)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        # Run OCR
        text = pytesseract.image_to_string(img, lang=lang)
        return text.strip()
    except Exception as e:
        logger.exception(f"OCR error on {image_path}")
        raise Exception(f"OCR failed: {str(e)}")

def extract_text_from_pdf(pdf_path, lang='eng'):
    """Extract text from a PDF by converting each page to an image and OCR'ing."""
    if not POPPLER_AVAILABLE:
        raise Exception("poppler-utils not installed. Cannot process PDFs. Install poppler-utils.")
    try:
        images = convert_from_path(pdf_path, dpi=300)
        all_text = []
        for i, img in enumerate(images, 1):
            # Convert image to RGB if needed
            if img.mode != 'RGB':
                img = img.convert('RGB')
            # OCR each page
            text = pytesseract.image_to_string(img, lang=lang)
            all_text.append(f"--- Page {i} ---\n{text}\n")
        return '\n'.join(all_text)
    except Exception as e:
        logger.exception(f"PDF OCR error on {pdf_path}")
        raise Exception(f"PDF OCR failed: {str(e)}")

def process_ocr_task(task_id, input_path, original_filename, lang):
    """Background task: perform OCR and save result."""
    task = load_task(task_id)
    if not task:
        return
    task['status'] = 'processing'
    task['progress'] = 0
    save_task(task_id, task)

    try:
        # Determine file type
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

        # Save text to file
        base, _ = os.path.splitext(original_filename)
        output_filename = f"{base}_ocr.txt"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)
        # Ensure unique name
        counter = 1
        while os.path.exists(output_path):
            output_filename = f"{base}_ocr_{counter}.txt"
            output_path = os.path.join(UPLOAD_FOLDER, output_filename)
            counter += 1

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)

        # Clean up input file
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
        # Clean up input file if exists
        if os.path.exists(input_path):
            os.remove(input_path)

def register_routes(app):
    @app.route('/ocr/upload', methods=['POST'])
    def ocr_upload():
        # Validate file
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Empty filename'}), 400

        # Check extension
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'error': f'Unsupported file type. Allowed: {", ".join(ALLOWED_EXTENSIONS)}'}), 400

        # Validate language
        lang = request.form.get('language', 'eng')
        if lang not in LANGUAGES:
            return jsonify({'error': f'Unsupported language: {lang}'}), 400

        # Check if OCR is available
        if not TESSERACT_AVAILABLE:
            return jsonify({'error': 'Tesseract is not installed on the server. Please contact administrator.'}), 500

        # Save uploaded file temporarily
        filename = secure_filename(file.filename)
        temp_path = os.path.join(UPLOAD_FOLDER, f"temp_{uuid.uuid4().hex}_{filename}")
        file.save(temp_path)

        # Create task
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

        # Start background thread
        threading.Thread(target=process_ocr_task, args=(task_id, temp_path, filename, lang), daemon=True).start()

        return jsonify({'task_id': task_id})

    @app.route('/ocr/languages')
    def ocr_languages():
        return jsonify(LANGUAGES)
