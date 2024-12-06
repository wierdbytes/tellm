FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt && \
    rm requirements.txt

COPY app.py /app/app.py

CMD ["python", "app.py"]