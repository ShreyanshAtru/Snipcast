FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pipeline.py /app/pipeline.py
ENTRYPOINT ["python", "/app/pipeline.py"]
