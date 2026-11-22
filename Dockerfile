FROM python:3.16-rc-slim-trixie
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends iputils-ping traceroute && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
