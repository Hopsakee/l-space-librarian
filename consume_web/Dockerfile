# The consume list web app. Build from the repo root:
#   docker compose -f consume_web/compose.yaml up -d --build
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Amsterdam
# Same pin as the PEP 723 block in app.py; fastcore comes with it.
RUN pip install --no-cache-dir python-fasthtml==0.14.12

WORKDIR /app
# Only what the app imports: the consume list and the schema.
COPY consume_selection/__init__.py consume_selection/db.py consume_selection/consume_list.py consume_selection/
COPY consume_web/app.py consume_web/

ENV HOST=0.0.0.0 PORT=8000 CONSUME_DB=/data/l-space.db
EXPOSE 8000
CMD ["python", "consume_web/app.py"]
