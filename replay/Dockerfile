FROM python:3.12-slim AS runtime

WORKDIR /service

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --timeout 300 -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 replay
USER replay

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test

USER root
ENV PYTHONPATH=/service
COPY requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir --timeout 300 -r requirements-dev.txt
COPY tests ./tests
RUN pytest -q tests

FROM runtime AS final
