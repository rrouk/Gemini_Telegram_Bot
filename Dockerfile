# Dockerfile

FROM python:3.11-slim
WORKDIR /app

# Копируем только код и зависимости
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все Python-скрипты, .env-файлы, и всю директорию 'data'
# Файлы данных будут перезаписаны через Volumes, но это скопирует все скрипты.
COPY . /app