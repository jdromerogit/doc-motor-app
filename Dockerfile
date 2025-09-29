FROM python:3.11-slim

# LibreOffice para convertir DOCX→PDF y qpdf para pikepdf
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    fonts-dejavu-core fonts-dejavu-extra \
    qpdf \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "app.py"]
