FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED 1
ENV PYTHONDONTWRITEBYTECODE 1

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data \
    && mkdir -p /app/sessions \
    && mkdir -p /app/temp_profile_photos \
    && mkdir -p /app/har_and_cookies \
    && mkdir -p /app/generated_media \
    && mkdir -p /app/chat_categories

COPY . .

CMD ["python", "main_app.py"]