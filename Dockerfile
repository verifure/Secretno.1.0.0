FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN test -f main.py || (echo "ОШИБКА: main.py не найден в образе" && ls -la && exit 1)

CMD ["python", "main.py"]
