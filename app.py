import os, io
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import boto3
import re

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from docxtpl import DocxTemplate

app = FastAPI(title="Docx Runner", version="1.0")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET_PLANTILLAS = os.getenv("BUCKET_PLANTILLAS", "")
BUCKET_RESULTADOS = os.getenv("BUCKET_RESULTADOS", "")

s3 = boto3.client("s3", region_name=AWS_REGION)

def slugify_filename(name: str) -> str:
    """
    Devuelve un nombre seguro para archivo (sin extensión).
    Si el nombre queda vacío, genera dummy_<timestamp>.
    """
    import time as _time
    base = re.sub(r"[^A-Za-z0-9_\- ]+", "", str(name or "")).strip().replace(" ", "_")
    return base if base else f"dummy_{int(_time.time())}"

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.api_route("/s3-test", methods=["GET", "POST"])
async def s3_test(
    request: Request,
    tenant_id: Optional[str] = "pe",
    template_id: Optional[str] = "pe_plantilla_solicitud_v2_test"
):
    """
    - GET: prueba de lectura de plantilla y escritura de dummy con timestamp (compatibilidad).
    - POST (recomendado): acepta JSON con {"filename": "..."} y sube un TXT con ese nombre.
    """
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    # 1) Verificar existencia de la plantilla
    plantilla_key = f"tenants/{tenant_id}/templates/{template_id}.docx"
    head_ok = False
    version_id = None
    try:
        head_obj = s3.head_object(Bucket=BUCKET_PLANTILLAS, Key=plantilla_key)
        head_ok = True
        version_id = head_obj.get("VersionId")
    except ClientError:
        head_ok = False

    # 2) Leer body (si viene) para controlar filename
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

    # 3) Definir nombre del archivo de resultado
    #    - Si POST con {"filename": "..."}: usarlo (sanitizado).
    #    - Si no, usar dummy_<timestamp>.
    fname_base = slugify_filename(body.get("filename") if isinstance(body, dict) else None)
    # Para GET y POST mantenemos el mismo patrón de ruta de resultados:
    # pruebas/{tenant}/{template}/{fname}.txt
    result_key = f"pruebas/{tenant_id}/{template_id}/{fname_base}.txt"

    # 4) Escribir objeto de prueba en el bucket de resultados
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

        # URL presignada
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
    Renderiza una plantilla DOCX (en BUCKET_PLANTILLAS) con docxtpl y sube el resultado
    (DOCX) a BUCKET_RESULTADOS. Si filename viene, se usa; si no, se genera con timestamp.
    """
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    tpl_key = f"tenants/{req.tenant_id}/templates/{req.template_id}.docx"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = slugify_filename(req.filename or f"{req.template_id}_{ts}")
    result_key = f"resultados/{req.tenant_id}/{fname}.docx"

    # 1) Descargar plantilla
    try:
        obj = s3.get_object(Bucket=BUCKET_PLANTILLAS, Key=tpl_key)
        template_bytes = obj["Body"].read()
    except ClientError as e:
        raise HTTPException(status_code=404, detail=f"No se pudo leer la plantilla: {str(e)}")

    # 2) Renderizar DOCX
    try:
        doc = DocxTemplate(io.BytesIO(template_bytes))
        doc.render(req.context)
        out_io = io.BytesIO()
        doc.save(out_io)
        out_io.seek(0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al renderizar DOCX: {str(e)}")

    # 3) Subir resultado
    try:
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=result_key,
            Body=out_io.getvalue(),
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo subir el resultado: {str(e)}")

    # 4) URL presignada
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": result_key},
            ExpiresIn=3600,
        )
    except ClientError:
        url = None

    return {
        "ok": True,
        "template_used": f"s3://{BUCKET_PLANTILLAS}/{tpl_key}",
        "result_key": result_key,
        "download_url": url,
    }
