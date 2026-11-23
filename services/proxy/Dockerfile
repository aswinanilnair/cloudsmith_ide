FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py .
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8080
CMD ["python", "proxy.py"]
