import os, io, tempfile, subprocess, re
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import boto3

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from docxtpl import DocxTemplate
import pikepdf

app = FastAPI(title="Docx Runner", version="1.1")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET_PLANTILLAS = os.getenv("BUCKET_PLANTILLAS", "")
BUCKET_RESULTADOS = os.getenv("BUCKET_RESULTADOS", "")

s3 = boto3.client("s3", region_name=AWS_REGION)

# ===== util: nombre seguro =====
def slugify_filename(name: str) -> str:
    import time as _time
    base = re.sub(r"[^A-Za-z0-9_\\- ]+", "", str(name or "")).strip().replace(" ", "_")
    return base if base else f"dummy_{int(_time.time())}"

# ===== util: convertir DOCX → PDF con LibreOffice headless =====
def docx_to_pdf_bytes(docx_bytes: bytes) -> bytes:
    # Guardar DOCX temporalmente y convertir con soffice
    with tempfile.TemporaryDirectory(prefix="docx_") as tmpdir:
        in_path = os.path.join(tmpdir, "in.docx")
        out_dir = tmpdir
        out_path = os.path.join(out_dir, "in.pdf")

        with open(in_path, "wb") as f:
            f.write(docx_bytes)

        # Convertir
        subprocess.check_call([
            "soffice", "--headless", "--norestore", "--nodefault", "--invisible",
            "--convert-to", "pdf:writer_pdf_Export", "--outdir", out_dir, in_path
        ])

        if not os.path.exists(out_path):
            raise RuntimeError("No se generó el PDF")

        with open(out_path, "rb") as f:
            return f.read()

# ===== util: proteger PDF (sin contraseña de usuario, permisos restringidos) =====
def protect_pdf_nocopy_noedit(pdf_bytes: bytes, owner_password: Optional[str] = None) -> bytes:
    """
    Aplica permisos 'no modificar/editar/extraer'. Se cifra con contraseña de propietario.
    El lector abre sin contraseña (user password = None), pero no debería permitir editar.
    OJO: algunos visores ignoran permisos; esto es disuasivo, no blindaje absoluto.
    """
    owner_pwd = owner_password or os.getenv("PDF_OWNER_PASSWORD", "OnlyOwnerCanEdit_ChangeMe!")
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        # Permisos: sin modificar, sin extraer, sin comentar, sin imprimir
        perms = pikepdf.Permissions(
            modify=False,
            annotate=False,
            extract=False,
            print=False,
            assemble=False,
            forms=False
        )
        out = io.BytesIO()
        pdf.save(out, encryption=pikepdf.Encryption(
            user=None,                   # sin password para abrir
            owner=owner_pwd,             # owner password
            allow=perms,
            R=4,                         # nivel de cifrado/compatibilidad
        ))
        return out.getvalue()

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

@app.api_route("/s3-test", methods=["GET", "POST"])
async def s3_test(
    request: Request,
    tenant_id: Optional[str] = "pe",
    template_id: Optional[str] = "pe_plantilla_solicitud_v2_test"
):
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    plantilla_key = f"tenants/{tenant_id}/templates/{template_id}.docx"
    head_ok = False
    version_id = None
    try:
        head_obj = s3.head_object(Bucket=BUCKET_PLANTILLAS, Key=plantilla_key)
        head_ok = True
        version_id = head_obj.get("VersionId")
    except ClientError:
        head_ok = False

    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

    fname_base = slugify_filename(body.get("filename") if isinstance(body, dict) else None)
    result_key = f"pruebas/{tenant_id}/{template_id}/{fname_base}.txt"

    escritura_ok = False
    presigned_url = None
    try:
        content_bytes = f"OK {datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=result_key,
            Body=content_bytes,
            ContentType="text/plain"
        )
        escritura_ok = True
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": result_key},
            ExpiresIn=3600,
        )
    except ClientError:
        escritura_ok = False

    return {
        "bucket_plantillas": BUCKET_PLANTILLAS,
        "bucket_resultados": BUCKET_RESULTADOS,
        "plantilla_key": plantilla_key,
        "version_id": version_id,
        "plantilla_head_ok": head_ok,
        "escritura_ok": escritura_ok,
        "result_key": result_key,
        "presigned_url": presigned_url,
    }

class RenderRequest(BaseModel):
    tenant_id: str = Field(..., example="pe")
    template_id: str = Field(..., example="pe_plantilla_solicitud_v2_test")
    context: Dict[str, Any] = Field(..., example={"nombre": "Juan", "edad": 30})
    filename: Optional[str] = None

@app.post("/render")
def render(req: RenderRequest):
    """
    Renderiza la plantilla DOCX, sube DOCX y PDF (protegido) a S3 y devuelve
    ambos links presignados. El PDF es el principal.
    - filename: prioridad 1) req.filename; 2) context["filename"]; 3) template_id
    - Siempre se añade timestamp al final.
    - PDF con permisos restringidos (no editar/copiar/imprimir).
    """
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    tpl_key = f"tenants/{req.tenant_id}/templates/{req.template_id}.docx"

    # Nombre base
    context_filename = req.context.get("filename") if isinstance(req.context, dict) else None
    base_name = req.filename or context_filename or req.template_id
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{slugify_filename(base_name)}_{ts}"

    # 1) Descargar plantilla
    try:
        obj = s3.get_object(Bucket=BUCKET_PLANTILLAS, Key=tpl_key)
        template_bytes = obj["Body"].read()
    except ClientError as e:
        raise HTTPException(status_code=404, detail=f"No se pudo leer la plantilla: {str(e)}")

    # 2) Renderizar DOCX en memoria
    try:
        doc = DocxTemplate(io.BytesIO(template_bytes))
        doc.render(req.context)
        out_io = io.BytesIO()
        doc.save(out_io)
        out_io.seek(0)
        docx_bytes = out_io.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al renderizar DOCX: {str(e)}")

    # 3) Convertir a PDF
    try:
        pdf_bytes = docx_to_pdf_bytes(docx_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al convertir a PDF: {str(e)}")

    # 4) Proteger PDF (no editar/copiar/imprimir)
    try:
        pdf_protected_bytes = protect_pdf_nocopy_noedit(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al proteger PDF: {str(e)}")

    # 5) Subir DOCX y PDF
    docx_key = f"resultados/{req.tenant_id}/{fname}.docx"
    pdf_key  = f"resultados/{req.tenant_id}/{fname}.pdf"

    try:
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=docx_key,
            Body=docx_bytes,
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=pdf_key,
            Body=pdf_protected_bytes,
            ContentType="application/pdf",
        )
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo subir el resultado: {str(e)}")

    # 6) URLs presignadas (prioriza PDF)
    try:
        url_docx = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": docx_key},
            ExpiresIn=3600,
        )
    except ClientError:
        url_docx = None

    try:
        url_pdf = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": pdf_key},
            ExpiresIn=3600,
        )
    except ClientError:
        url_pdf = None

    return {
        "ok": True,
        "template_used": f"s3://{BUCKET_PLANTILLAS}/{tpl_key}",
        "filename_base": fname,
        "result_keys": {"docx": docx_key, "pdf": pdf_key},
        "download_url_pdf": url_pdf,   # <- principal (el que mostrarás/entregarás)
        "download_url_docx": url_docx, # <- backup / auditoría
    }
