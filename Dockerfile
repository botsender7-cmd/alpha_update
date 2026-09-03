FROM python:3.12-slim

# ffmpeg is a runtime dependency (subprocess calls to `ffmpeg` in app.py) —
# not in requirements.txt because it's a binary, not a pip package.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

EXPOSE 5000

# Flask's dev server (app.run(debug=True)) is not fit for a container:
# debug=True enables the Werkzeug debugger, which is a remote code
# execution risk if this port is ever exposed. Run via gunicorn instead
# and ignore the `if __name__ == "__main__"` block entirely.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "300", "app:app"]
