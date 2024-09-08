FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml ./
COPY incidentpilot ./incidentpilot
RUN pip install . && useradd -u 10001 -m app && mkdir /data && chown app /data
USER 10001
EXPOSE 8000
CMD ["incidentpilot", "serve"]
