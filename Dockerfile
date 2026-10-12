FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data TZ=Europe/Lisbon
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY public ./public
COPY wsgi.py .

RUN useradd --system --uid 1000 solar && mkdir -p /data && chown solar /data
USER solar
VOLUME ["/data"]
EXPOSE 8000

# um só processo, para a sincronização diária correr uma única vez
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
