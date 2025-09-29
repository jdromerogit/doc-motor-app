import os, io, time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from docxtpl import DocxTemplate

app = FastAPI(title="Docx Runner", version="1.0")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BUCKET_PLANTILLAS = os.getenv("BUCKET_PLANTILLAS", "")
BUCKET_RESULTADOS = os.getenv("BUCKET_RESULTADOS", "")

s3 = boto3.client("s3", region_name=AWS_REGION)


@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/s3-test")
def s3_test(tenant_id: Optional[str] = "pe", template_id: Optional[str] = "pe_plantilla_solicitud_v2_test"):
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    plantilla_key = f"tenants/{tenant_id}/templates/{template_id}.docx"
    head_ok, escritura_ok, presigned_url = False, False, None

    try:
        s3.head_object(Bucket=BUCKET_PLANTILLAS, Key=plantilla_key)
        head_ok = True
    except ClientError:
        head_ok = False

    dummy_key = f"pruebas/{tenant_id}/{template_id}/dummy_{int(time.time())}.txt"
    try:
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=dummy_key,
            Body=b"dummy ok",
            ContentType="text/plain"
        )
        escritura_ok = True
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": dummy_key},
            ExpiresIn=3600,
        )
    except ClientError:
        escritura_ok = False

    return {
        "bucket_plantillas": BUCKET_PLANTILLAS,
        "bucket_resultados": BUCKET_RESULTADOS,
        "plantilla_key": plantilla_key,
        "plantilla_head_ok": head_ok,
        "escritura_ok": escritura_ok,
        "presigned_url": presigned_url,
    }


class RenderRequest(BaseModel):
    tenant_id: str = Field(..., example="pe")
    template_id: str = Field(..., example="pe_plantilla_solicitud_v2_test")
    context: Dict[str, Any] = Field(..., example={"nombre": "Juan", "edad": 30})
    filename: Optional[str] = None


@app.post("/render")
def render(req: RenderRequest):
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS o BUCKET_RESULTADOS")

    tpl_key = f"tenants/{req.tenant_id}/templates/{req.template_id}.docx"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = req.filename or f"{req.template_id}_{ts}"
    result_key = f"resultados/{req.tenant_id}/{fname}.docx"

    try:
        obj = s3.get_object(Bucket=BUCKET_PLANTILLAS, Key=tpl_key)
        template_bytes = obj["Body"].read()
    except ClientError as e:
        raise HTTPException(status_code=404, detail=f"No se pudo leer la plantilla: {str(e)}")

    try:
        doc = DocxTemplate(io.BytesIO(template_bytes))
        doc.render(req.context)
        out_io = io.BytesIO()
        doc.save(out_io)
        out_io.seek(0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al renderizar DOCX: {str(e)}")

    try:
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=result_key,
            Body=out_io.getvalue(),
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"No se pudo subir el resultado: {str(e)}")

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
