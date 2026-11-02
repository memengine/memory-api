FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml /app/
COPY api /app/api
RUN python -m pip install --no-cache-dir .
COPY alembic.ini /app/
COPY docs/extraction_spec.md /app/docs/extraction_spec.md
COPY beta/bootstrap.py /app/beta/bootstrap.py
COPY beta/local_vector_publisher.py /app/local_vector_publisher.py
RUN groupadd --gid 1000 memoryos && useradd --uid 1000 --gid 1000 --create-home memoryos
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
