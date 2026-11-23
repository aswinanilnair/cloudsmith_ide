FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .
# Runs as root because it uses the host's Docker socket. Workspaces never get the
# socket; they run as the non-root "coder" user with no capabilities.
CMD ["python", "agent.py"]