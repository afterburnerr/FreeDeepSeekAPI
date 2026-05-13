# FreeDeepSeekAPI — OpenAI-compatible proxy for chat.deepseek.com.
# This image ships Chrome + Xvfb + DrissionPage so the built-in
# refresh_cookies.sh can renew the AWS WAF clearance cookie from inside
# the container — no host access required.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKERMODE=true \
    DISPLAY=:99

# System deps:
#  * google-chrome-stable — used by DrissionPage for the WAF bypass
#  * xvfb                  — virtual display for headless Chrome
#  * curl/ca-certificates  — fetching Chrome + healthcheck
#  * build-essential       — some pip wheels need a compiler
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg wget \
      xvfb xauth \
      fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
      libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 \
      libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
      libxext6 libxfixes3 libxkbcommon0 libxrandr2 libu2f-udev \
      build-essential \
 && curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
      -o /tmp/chrome.deb \
 && apt-get install -y /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
# Pin setuptools<81 for pkg_resources (still imported by dsk/api.py).
# DrissionPage + pyvirtualdisplay are for cookie bypass.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir \
      "setuptools<81" \
      DrissionPage==4.1.0.18 \
      pyvirtualdisplay==3.0 \
      requests

COPY . .
RUN chmod +x refresh_cookies.sh

ENV HOST=0.0.0.0 \
    PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8080/v1/models > /dev/null || exit 1

CMD ["python", "main.py"]
