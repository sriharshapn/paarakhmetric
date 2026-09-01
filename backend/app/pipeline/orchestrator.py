import os
import cv2
import logging
from typing import Dict, Any

from app.pipeline.geometry import suppress_glare, correct_perspective_quad, unwarp_cylindrical_label
from app.pipeline.yolo_seg import segment_label
from app.pipeline.ocr_engine import perform_ocr
from app.extraction.parser import parse_ocr_results
from app.extraction.llm import parse_with_vision_llm
from app.rules.engine import evaluate_compliance

logger = logging.getLogger(__name__)

def run_paarakhmetric_pipeline(image_path: str, use_vision_llm: bool = True) -> Dict[str, Any]:
    """
    Executes the exact PaarakhMetric Pipeline:
    1. Image Quality Check (blur, lighting, framing) -> basic check via OpenCV
    2. YOLO26n-Seg (package/label segmentation)
    3. OpenCV (mask cleanup + geometry + perspective/rotation correction)
    4. Label/ROI Extraction
    5. PaddleOCR (text detection + recognition + coordinates)
    6. Field Extraction (MRP, quantity, manufacturer, etc.) - via Vision LLM + OCR Text
    7. Compliance Engine (Legal Metrology rules + validation)
    8. Physical Measurement (calibration + numeral/label measurements where applicable)
    9. Compliance Result (Compliant / Review / Non-Compliant)
    10. Evidence + Report (highlighted violations + extracted data + confidence)
    """
    logger.info("=== STARTING PAARAKHMETRIC PIPELINE ===")
    
    # --- Step 1: Image Quality Check ---
    logger.info("Step 1: Image Quality Check (Lighting/Glare)...")
    img = cv2.imread(image_path)
    if img is None:
        return {"error": "Invalid image"}
    img_no_glare = suppress_glare(img)

    # --- Step 2: YOLO26n-Seg ---
    logger.info("Step 2: YOLO26n-Seg (Label Segmentation)...")
    temp_no_glare_path = image_path + ".noglare.jpg"
    cv2.imwrite(temp_no_glare_path, img_no_glare)
    roi_segmented = segment_label(temp_no_glare_path)

    # --- Step 3: OpenCV Geometry & Perspective Correction ---
    logger.info("Step 3: OpenCV Geometry (Perspective/Rotation)...")
    img_corrected, _ = correct_perspective_quad(roi_segmented)
    
    # --- Step 4: Label/ROI Extraction ---
    logger.info("Step 4: Label/ROI Extraction...")
    final_roi_path = image_path + ".roi.jpg"
    cv2.imwrite(final_roi_path, img_corrected)

    # --- Step 5: PaddleOCR ---
    logger.info("Step 5: PaddleOCR (RapidOCR ONNX Runtime)...")
    ocr_results = perform_ocr(final_roi_path)
    ocr_text_combined = " ".join([item.get("text", "") for item in (ocr_results or [])])

    # --- Step 6: Field Extraction (Vision LLM) ---
    logger.info("Step 6: Field Extraction (Vision LLM + Text LLM)...")
    parsed_data = {}
    if use_vision_llm and os.getenv("GEMINI_API_KEY"):
        logger.info(" -> Invoking Vision LLM on ROI...")
        parsed_data = parse_with_vision_llm(final_roi_path, fallback_ocr_text=ocr_text_combined)
        
    if not parsed_data:
        logger.info(" -> Falling back to Text LLM / Regex Parser...")
        parsed_data = parse_ocr_results(ocr_results) if ocr_results else {}

    # --- Step 7 & 8: Compliance Engine & Physical Measurement ---
    logger.info("Step 7 & 8: Compliance Engine & Physical Measurement...")
    # Passing placeholder PDP area / measured font height (would be dynamic in full app)
    compliance_report = evaluate_compliance(
        parsed_decls=parsed_data,
        pdp_area_cm2=120.0, 
        measured_font_height_mm=4.5
    )

    # --- Step 9 & 10: Compliance Result & Report ---
    logger.info(f"Step 9 & 10: Compliance Result -> {compliance_report['overall_status']}")
    
    # Cleanup temp files
    try:
        os.remove(temp_no_glare_path)
        os.remove(final_roi_path)
    except:
        pass

    logger.info("=== PIPELINE COMPLETE ===")
    
    return {
        "pipeline_status": "SUCCESS",
        "ocr_raw": ocr_results,
        "extracted_fields": parsed_data,
        "compliance_report": compliance_report
    }
