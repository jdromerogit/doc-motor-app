import os, time
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException

app = FastAPI()

AWS_REGION        = os.getenv("AWS_REGION", "us-east-1")
BUCKET_PLANTILLAS = os.getenv("BUCKET_PLANTILLAS", os.getenv("S3_BUCKET_PLANTILLAS", ""))
BUCKET_RESULTADOS = os.getenv("BUCKET_RESULTADOS", os.getenv("S3_BUCKET_RESULTADOS", ""))

s3 = boto3.client("s3", region_name=AWS_REGION)

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/s3-test")
def s3_test(tenant_id: Optional[str] = "pe", template_id: Optional[str] = "pe_plantilla_solicitud_v2_test"):
    if not BUCKET_PLANTILLAS or not BUCKET_RESULTADOS:
        raise HTTPException(status_code=500, detail="Faltan BUCKET_PLANTILLAS/RESULTADOS")

    plantilla_key = f"tenants/{tenant_id}/templates/{template_id}.docx"

    # 1) HEAD de la plantilla
    head_ok = False
    try:
        s3.head_object(Bucket=BUCKET_PLANTILLAS, Key=plantilla_key)
        head_ok = True
    except ClientError:
        head_ok = False

    # 2) PUT de un dummy en resultados
    epoch = int(time.time())
    dummy_key = f"pruebas/{tenant_id}/{template_id}/dummy_{epoch}.txt"
    try:
        s3.put_object(
            Bucket=BUCKET_RESULTADOS,
            Key=dummy_key,
            Body=b"dummy ok",
            ContentType="text/plain"
        )
        escritura_ok = True
    except ClientError:
        escritura_ok = False

    # 3) Presigned URL
    presigned_url = None
    try:
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": BUCKET_RESULTADOS, "Key": dummy_key},
            ExpiresIn=900,  # 15 min
        )
    except ClientError:
        presigned_url = None

    return {
        "bucket_plantillas": BUCKET_PLANTILLAS,
        "bucket_resultados": BUCKET_RESULTADOS,
        "plantilla_key": plantilla_key,
        "plantilla_head_ok": head_ok,
        "escritura_ok": escritura_ok,
        "presigned_url": presigned_url,
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info", proxy_headers=True)

