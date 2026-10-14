# Image for the project's own services (MCP servers now, apps later).
# Project code is mounted at /opt/project at runtime, so code changes need a restart, not a rebuild.
FROM python:3.14-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /opt/project
ENV PYTHONPATH=/opt/project/src PYTHONUNBUFFERED=1
