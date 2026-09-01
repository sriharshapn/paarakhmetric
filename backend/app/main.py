from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Optional
from rapidfuzz import fuzz
import os
import shutil

from app.database import init_db, get_db, SessionLocal, User, Product, Inspection, ProductImage, Declaration, ComplianceResult, OCRResult, StatutoryRule, sync_inspection_fts
from app.schemas import UserResponse, UserCreate, ProductResponse, ProductCreate, InspectionResponse, InspectionCreate
from app.auth import hash_password, verify_password, create_access_token, get_current_user, get_current_user_optional

# Pipeline imports
from app.pipeline.quality import analyze_image_quality
from app.pipeline.preprocess import correct_skew, apply_contrast_enhancement
from app.pipeline.geometry import calculate_pdp_scale, suppress_glare, correct_perspective_quad, unwarp_cylindrical_label
from app.pipeline.dimensions import calculate_font_dimensions, evaluate_rule_8_clearance
from app.pipeline.ocr_engine import perform_ocr
from app.extraction.parser import parse_ocr_results
from app.rules.engine import evaluate_compliance, classify_commodity
from app.pipeline.pdf_report import generate_pdf_report

app = FastAPI(title="PaarakhMetric Backend", version="1.0")

# Enable CORS for frontend requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "./uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def format_inspection_summary(insp: Inspection, match_score: float = 100.0) -> dict:
    """Format an inspection record for frontend consumption with match score."""
    decls = []
    for d in insp.declarations:
        decls.append({
            "field_name": d.field_name,
            "value": d.value or "",
            "status": d.status,
            "confidence": d.confidence or 0.0,
            "original_text": d.original_text or ""
        })
    rules = []
    for r in insp.compliance_results:
        rules.append({
            "rule_id": r.rule_id,
            "field": r.rule_id.split("-")[1].lower() if "-" in r.rule_id else "general",
            "status": r.status,
            "details": r.details or ""
        })
    return {
        "id": insp.id,
        "product": {
            "name": insp.product.name if insp.product else "Unknown Product",
            "manufacturer": insp.product.manufacturer if insp.product else "Unknown",
            "category": insp.product.category if insp.product else "General",
            "barcode": insp.product.barcode if insp.product else "N/A"
        },
        "timestamp": insp.timestamp.isoformat() if insp.timestamp else "",
        "status": insp.status,
        "location": insp.location or "Warehouse / Field Store",
        "officer": "Officer Shrey",
        "declarations": decls,
        "compliance_results": rules,
        "notes": insp.notes or "",
        "match_score": round(match_score, 1)
    }

@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        # Create or update default officer with Argon2id hash
        default_user = db.query(User).filter(User.username == "officer_shrey").first()
        if not default_user:
            default_officer = User(
                username="officer_shrey",
                hashed_password=hash_password("password123"),
                role="officer"
            )
            db.add(default_officer)
            db.commit()
        else:
            # Upgrade legacy plaintext password to Argon2id if needed
            if not default_user.hashed_password.startswith("$argon2"):
                default_user.hashed_password = hash_password("password123")
                db.commit()

        # Seed sample baseline inspections if database is empty
        if db.query(Inspection).count() == 0:
            officer = db.query(User).filter(User.username == "officer_shrey").first()
            
            # 1. Premium Basmati Rice (Compliant)
            p1 = Product(name="Premium Basmati Rice", manufacturer="India Foods Ltd", category="Grain", barcode="8901234567890")
            db.add(p1)
            db.commit()
            db.refresh(p1)

            i1 = Inspection(product_id=p1.id, officer_id=officer.id, status="COMPLIANT", location="Warehouse A, New Delhi", notes="All mandatory declarations present.")
            db.add(i1)
            db.commit()
            db.refresh(i1)

            db.add_all([
                Declaration(inspection_id=i1.id, field_name="mrp", value="₹240", status="VALIDATED", confidence=0.98, original_text="MRP Rs 240.00"),
                Declaration(inspection_id=i1.id, field_name="net_quantity", value="5 kg", status="VALIDATED", confidence=0.96, original_text="NET QUANTITY 5 kg"),
                Declaration(inspection_id=i1.id, field_name="manufacturer", value="India Foods Ltd", status="VALIDATED", confidence=0.95, original_text="Mfd by India Foods Ltd"),
                Declaration(inspection_id=i1.id, field_name="packing_date", value="07/2026", status="VALIDATED", confidence=0.94, original_text="PKD 07/2026"),
                Declaration(inspection_id=i1.id, field_name="consumer_care", value="1800-111-222", status="VALIDATED", confidence=0.91, original_text="Care No: 1800-111-222"),
                ComplianceResult(inspection_id=i1.id, rule_id="PC-MRP-001", status="PASS", details="MRP declaration present and validly formatted (₹240)"),
                ComplianceResult(inspection_id=i1.id, rule_id="PC-QTY-002", status="PASS", details="Net quantity is declared in standard units (kg)"),
                ComplianceResult(inspection_id=i1.id, rule_id="PC-DATE-003", status="PASS", details="Packing date present and valid (07/2026)"),
                ComplianceResult(inspection_id=i1.id, rule_id="PC-CARE-004", status="PASS", details="Customer care details detected")
            ])
            db.commit()
            sync_inspection_fts(db, i1.id)

            # 2. Choco Bites Family Pack (Non-Compliant)
            p2 = Product(name="Choco Bites Family Pack", manufacturer="Sweet Treats Inc", category="Confectionery", barcode="8902345678901")
            db.add(p2)
            db.commit()
            db.refresh(p2)

            i2 = Inspection(product_id=p2.id, officer_id=officer.id, status="NON_COMPLIANT", location="Reliance Store, Mumbai", notes="Missing consumer care contact information.")
            db.add(i2)
            db.commit()
            db.refresh(i2)

            db.add_all([
                Declaration(inspection_id=i2.id, field_name="mrp", value="₹150", status="VALIDATED", confidence=0.97, original_text="MRP ₹150"),
                Declaration(inspection_id=i2.id, field_name="net_quantity", value="400 g", status="VALIDATED", confidence=0.95, original_text="Net Wt. 400g"),
                Declaration(inspection_id=i2.id, field_name="packing_date", value="05/2026", status="VALIDATED", confidence=0.93, original_text="PACKED 05/26"),
                Declaration(inspection_id=i2.id, field_name="consumer_care", value="", status="POTENTIAL_VIOLATION", confidence=0.0, original_text="No match found"),
                ComplianceResult(inspection_id=i2.id, rule_id="PC-MRP-001", status="PASS", details="MRP declaration present and valid"),
                ComplianceResult(inspection_id=i2.id, rule_id="PC-QTY-002", status="PASS", details="Net quantity declared in standard units (g)"),
                ComplianceResult(inspection_id=i2.id, rule_id="PC-CARE-004", status="FAIL", details="Consumer care helpline/email missing (Rule 6(1)(n))")
            ])
            db.commit()
            sync_inspection_fts(db, i2.id)

    finally:
        db.close()

@app.get("/")
def read_root():
    return {"message": "Welcome to PaarakhMetric API"}

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "PaarakhMetric Backend", "auth": "Argon2id + JWT", "search": "SQLite FTS5 + RapidFuzz"}

# --- User & Auth Routes ---
@app.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already exists")
    new_user = User(
        username=user.username,
        hashed_password=hash_password(user.password),
        role=user.role
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@app.get("/users", response_model=List[UserResponse])
def get_users(db: Session = Depends(get_db)):
    return db.query(User).all()

@app.post("/auth/login")
def login(user: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if not db_user or not verify_password(db_user.hashed_password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    # Generate cryptographic JWT access token
    token = create_access_token(data={
        "sub": db_user.username,
        "role": db_user.role,
        "user_id": db_user.id
    })
    return {
        "message": "Login successful",
        "access_token": token,
        "token_type": "bearer",
        "username": db_user.username,
        "role": db_user.role
    }

# --- Product Routes ---
@app.post("/products", response_model=ProductResponse)
def create_product(product: ProductCreate, db: Session = Depends(get_db)):
    db_prod = Product(
        name=product.name,
        manufacturer=product.manufacturer,
        category=product.category,
        barcode=product.barcode
    )
    db.add(db_prod)
    db.commit()
    db.refresh(db_prod)
    return db_prod

@app.get("/products", response_model=List[ProductResponse])
def get_products(db: Session = Depends(get_db)):
    return db.query(Product).all()

# --- Inspection Routes & Search ---
@app.get("/inspections/search")
def search_inspections(
    q: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """
    Lightning-fast SQLite FTS5 + RapidFuzz hybrid search across historical inspections.
    Matches product names, manufacturers, barcodes, categories, raw OCR text, and violations.
    """
    all_inspections = db.query(Inspection).all()
    if not all_inspections:
        return []

    # If query is empty, filter by status and category
    if not q or not q.strip():
        results = all_inspections
        if status and status.upper() != "ALL":
            results = [i for i in results if (i.status or "").upper() == status.upper()]
        if category and category.upper() != "ALL":
            results = [i for i in results if (i.product.category if i.product else "").lower() == category.lower()]
        
        return [format_inspection_summary(i, match_score=100.0) for i in results[:limit]]

    query_str = q.strip()
    
    # 1. SQLite FTS5 Inverted Index Query
    fts_matched_ids = set()
    fts_scores = {}
    try:
        clean_tokens = [t.replace('"', '').replace("'", '').replace('*', '') for t in query_str.split() if t]
        if clean_tokens:
            fts_match_query = " ".join([f'"{token}"*' for token in clean_tokens])
            fts_rows = db.execute(text("""
                SELECT inspection_id, bm25(inspections_fts) as rank
                FROM inspections_fts
                WHERE inspections_fts MATCH :query
                ORDER BY rank
                LIMIT :limit
            """), {"query": fts_match_query, "limit": limit}).fetchall()
            for r in fts_rows:
                fts_matched_ids.add(r[0])
                fts_scores[r[0]] = max(60.0, 100.0 - abs(float(r[1]) * 10))
    except Exception:
        pass

    # 2. RapidFuzz Typo & OCR Error Matching
    scored_results = []
    for insp in all_inspections:
        if status and status.upper() != "ALL" and (insp.status or "").upper() != status.upper():
            continue
        if category and category.upper() != "ALL" and (insp.product.category if insp.product else "").lower() != category.lower():
            continue

        prod_name = (insp.product.name if insp.product else "") or ""
        mfr = (insp.product.manufacturer if insp.product else "") or ""
        barcode = (insp.product.barcode if insp.product else "") or ""
        location = insp.location or ""
        
        ocr_texts = [ocr.text for img in insp.images for ocr in img.ocr_results if ocr.text]
        ocr_blob = " ".join(ocr_texts)
        
        r_name = fuzz.partial_ratio(query_str.lower(), prod_name.lower())
        r_mfr = fuzz.partial_ratio(query_str.lower(), mfr.lower())
        r_ocr = fuzz.partial_ratio(query_str.lower(), ocr_blob.lower()) if ocr_blob else 0
        r_barcode = 100 if query_str in barcode else 0
        r_location = fuzz.partial_ratio(query_str.lower(), location.lower())

        max_fuzzy = max(r_name, r_mfr, r_ocr, r_barcode, r_location)
        is_fts_match = insp.id in fts_matched_ids
        fts_score = fts_scores.get(insp.id, 0)
        
        final_score = 0
        if is_fts_match:
            final_score = max(fts_score, max_fuzzy)
        elif max_fuzzy >= 65:  # Typo threshold
            final_score = max_fuzzy
            
        if final_score > 0 or query_str.lower() in prod_name.lower() or query_str.lower() in mfr.lower():
            scored_results.append((insp, final_score or 75.0))

    # Sort descending by match score
    scored_results.sort(key=lambda x: x[1], reverse=True)
    return [format_inspection_summary(insp, score) for insp, score in scored_results[:limit]]

@app.post("/inspections", response_model=InspectionResponse)
def create_inspection(inspection: InspectionCreate, db: Session = Depends(get_db)):
    default_officer = db.query(User).first()
    if not default_officer:
        default_officer = User(username="officer_default", hashed_password=hash_password("password"), role="officer")
        db.add(default_officer)
        db.commit()
        db.refresh(default_officer)

    db_inspection = Inspection(
        product_id=inspection.product_id,
        officer_id=default_officer.id,
        location=inspection.location,
        notes=inspection.notes,
        status="REQUIRES_REVIEW"
    )
    db.add(db_inspection)
    db.commit()
    db.refresh(db_inspection)
    sync_inspection_fts(db, db_inspection.id)
    return db_inspection

@app.get("/inspections", response_model=List[InspectionResponse])
def get_inspections(db: Session = Depends(get_db)):
    return db.query(Inspection).all()

@app.get("/inspections/{inspection_id}", response_model=InspectionResponse)
def get_inspection(inspection_id: int, db: Session = Depends(get_db)):
    db_inspection = db.query(Inspection).filter(Inspection.id == inspection_id).first()
    if not db_inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
    return db_inspection

@app.delete("/inspections/{inspection_id}")
def delete_inspection(inspection_id: int, db: Session = Depends(get_db)):
    db_inspection = db.query(Inspection).filter(Inspection.id == inspection_id).first()
    if not db_inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
    
    # 1. Delete associated declarations & compliance results
    db.query(Declaration).filter(Declaration.inspection_id == inspection_id).delete()
    db.query(ComplianceResult).filter(ComplianceResult.inspection_id == inspection_id).delete()
    
    # 2. Delete images & OCR results
    images = db.query(ProductImage).filter(ProductImage.inspection_id == inspection_id).all()
    for img in images:
        db.query(OCRResult).filter(OCRResult.product_image_id == img.id).delete()
        if os.path.exists(img.filepath):
            try:
                os.remove(img.filepath)
            except Exception:
                pass
    db.query(ProductImage).filter(ProductImage.inspection_id == inspection_id).delete()
    
    # 3. Remove from FTS5 inverted search index
    try:
        db.execute(text("DELETE FROM inspections_fts WHERE inspection_id = :id"), {"id": inspection_id})
    except Exception:
        pass
        
    db.delete(db_inspection)
    db.commit()
    return {"message": f"Inspection #{inspection_id} successfully deleted", "id": inspection_id}

# --- Image Upload & E2E Process Pipeline ---
@app.post("/inspections/{inspection_id}/upload-image")
async def upload_image(
    inspection_id: int,
    panel_side: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # Verify inspection exists
    db_inspection = db.query(Inspection).filter(Inspection.id == inspection_id).first()
    if not db_inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
    
    # 1. Save uploaded file
    filename = f"{inspection_id}_{panel_side}_{file.filename}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Save Image details in database
    db_image = ProductImage(
        inspection_id=inspection_id,
        filename=filename,
        filepath=filepath,
        panel_side=panel_side
    )
    db.add(db_image)
    db.commit()
    db.refresh(db_image)
    
        # --- EXECUTE EXACT PAARAKHMETRIC PIPELINE ---
    from app.pipeline.orchestrator import run_paarakhmetric_pipeline
    
    pipeline_result = run_paarakhmetric_pipeline(filepath, use_vision_llm=True)
    
    parsed_decls = pipeline_result.get('extracted_fields', {})
    compliance = pipeline_result.get('compliance_report', {})
    ocr_raw = pipeline_result.get('ocr_raw', [])
    
    # Save OCR words/bboxes into DB for audit
    if ocr_raw:
        for item in ocr_raw:
            try:
                db_ocr = OCRResult(
                    product_image_id=db_image.id,
                    text=item.get('text', ''),
                    confidence=item.get('confidence', 0.0),
                    bbox_x=item.get('bounding_box', {}).get('x', 0),
                    bbox_y=item.get('bounding_box', {}).get('y', 0),
                    bbox_w=item.get('bounding_box', {}).get('width', 0),
                    bbox_h=item.get('bounding_box', {}).get('height', 0)
                )
                db.add(db_ocr)
            except Exception as e:
                pass
        db.commit()

    # Extract declarations & Automatic Commodity Categorization
    all_ocr_text = " ".join([item.get('text', '') for item in (ocr_raw or [])])
    detected_name = parsed_decls.get('product_name', {}).get('value') or ''
    detected_category = classify_commodity(all_ocr_text, detected_name)
    
    if db_inspection.product:
        if not db_inspection.product.name or db_inspection.product.name == 'New Unidentified Package':
            db_inspection.product.name = detected_name or 'Packaged Commodity'
        if not db_inspection.product.category or db_inspection.product.category == 'General':
            db_inspection.product.category = detected_category
        db.commit()
    
    # Save or update parsed declarations
    for field_name, decl in parsed_decls.items():
        if field_name == 'unsupported_language_detected':
            continue
        existing = db.query(Declaration).filter(
            Declaration.inspection_id == inspection_id,
            Declaration.field_name == field_name
        ).first()
        
        if existing:
            existing.value = decl.get('value', '')
            existing.status = decl.get('status', 'MISSING')
            existing.confidence = decl.get('confidence', 0.0)
            existing.original_text = decl.get('original_text', '')
        else:
            db_decl = Declaration(
                inspection_id=inspection_id,
                field_name=field_name,
                value=decl.get('value', ''),
                status=decl.get('status', 'MISSING'),
                confidence=decl.get('confidence', 0.0),
                original_text=decl.get('original_text', '')
            )
            db.add(db_decl)
    db.commit()

    # Update overall status
    overall_status = compliance.get('overall_status', 'REQUIRES_REVIEW')
    db_inspection.status = overall_status
    db.commit()

    # Log to Compliance Result
    for res in compliance.get('results', []):
        db_res = ComplianceResult(
            inspection_id=inspection_id,
            rule_id=res.get('rule_id'),
            status=res.get('status'),
            details=res.get('details')
        )
        db.add(db_res)
    db.commit()

    return {
        'status': 'success',
        'image_id': db_image.id,
        'image_url': f'/uploads/{filename}',
        'extracted_data': parsed_decls,
        'compliance_report': compliance
    }

@app.get("/inspections/{inspection_id}/pdf-report")
def get_pdf_report(inspection_id: int, db: Session = Depends(get_db)):
    db_inspection = db.query(Inspection).filter(Inspection.id == inspection_id).first()
    if not db_inspection:
        raise HTTPException(status_code=404, detail="Inspection not found")
        
    # Serialize data for PDF engine
    product_data = {
        "name": db_inspection.product.name or "N/A",
        "manufacturer": db_inspection.product.manufacturer or "N/A",
        "category": db_inspection.product.category or "N/A",
        "barcode": db_inspection.product.barcode or "N/A"
    }
    
    decls_data = []
    for d in db_inspection.declarations:
        decls_data.append({
            "field_name": d.field_name,
            "value": d.value or "",
            "confidence": d.confidence or 0.0,
            "status": d.status
        })
        
    rules_data = []
    for r in db_inspection.compliance_results:
        rules_data.append({
            "rule_id": r.rule_id,
            "status": r.status,
            "details": r.details or ""
        })
        
    # Find all uploaded product images across all packaging panels
    images_records = db.query(ProductImage).filter(ProductImage.inspection_id == inspection_id).order_by(ProductImage.id.asc()).all()
    images_list = []
    for img in images_records:
        if os.path.exists(img.filepath):
            images_list.append({
                "panel_side": img.panel_side,
                "filepath": img.filepath,
                "filename": img.filename
            })

    pdf_payload = {
        "id": db_inspection.id,
        "timestamp": db_inspection.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "status": db_inspection.status,
        "officer": "Officer Shrey",  # Default for MVP
        "location": db_inspection.location or "N/A",
        "notes": db_inspection.notes or "",
        "product": product_data,
        "declarations": decls_data,
        "compliance_results": rules_data,
        "images": images_list,
        "image_filepath": images_list[0]["filepath"] if images_list else None
    }
    
    output_dir = "./reports"
    os.makedirs(output_dir, exist_ok=True)
    pdf_path = os.path.join(output_dir, f"report_{inspection_id}.pdf")
    
    try:
        generate_pdf_report(pdf_payload, pdf_path)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Failed to generate PDF: {err}")
        
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"PaarakhMetric_Report_{inspection_id}.pdf")

@app.get("/rules")
def get_rules(db: Session = Depends(get_db)):
    """Retrieve all 17 statutory Legal Metrology rules from the database."""
    rules = db.query(StatutoryRule).order_by(StatutoryRule.rule_number).all()
    return [{
        "rule_number": r.rule_number,
        "rule_id": r.rule_id,
        "code": r.code,
        "field": r.field,
        "name": r.name,
        "year": r.year,
        "required": r.required,
        "severity": r.severity,
        "description": r.description,
        "source": r.source,
        "applies_to": r.applies_to
    } for r in rules]

# Serve frontend build if present
from fastapi.staticfiles import StaticFiles
frontend_dist = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../frontend/dist"))
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

