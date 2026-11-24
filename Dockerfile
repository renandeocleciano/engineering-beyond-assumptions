FROM python:3.13-slim
WORKDIR /code
COPY pyproject.toml README.md ./
COPY app ./app
COPY experiments ./experiments
RUN pip install --no-cache-dir '.[dev]'
COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
