FROM python:3.12-slim

WORKDIR /app
# Flat-upload friendly: the whole app tree lives in app.tar.gz (ADD auto-extracts it into /app).
ADD app.tar.gz /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

ENV DATA_DIR=/tmp/data PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
