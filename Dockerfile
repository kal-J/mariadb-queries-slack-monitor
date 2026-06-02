FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir pymysql

COPY mariadb_monitor.py .

RUN useradd --system --no-create-home --shell /usr/sbin/nologin monitor \
 && mkdir -p /var/lib/mariadb-monitor \
 && chown monitor:monitor /var/lib/mariadb-monitor

USER monitor

CMD ["python3", "mariadb_monitor.py"]
