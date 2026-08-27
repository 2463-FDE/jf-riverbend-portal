# P5 follow-up, item 3: python:3.12-slim floats to whatever Debian release is
# current (trixie/13 as of this pin, which still carries HIGH-severity
# util-linux/bsdutils/OpenSSL findings). python:3.12-slim-bookworm (Debian
# 12) scanned fully clean (0 CRITICAL/HIGH) at pin time; explicit variant +
# digest so this doesn't silently float again.
FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8076
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8076"]
