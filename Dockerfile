FROM python:3.12-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends cups cups-client printer-driver-hpcups && rm -rf /var/lib/apt/lists/* && useradd --system --no-create-home --gid lp appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py cups_control.py entrypoint.sh ./
COPY static ./static
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
