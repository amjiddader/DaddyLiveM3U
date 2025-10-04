FROM python:3.12-slim-bullseye
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN echo "Now starting..."
RUN git clone https://github.com/amjiddader/DaddyLiveM3U.git /app
WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN chmod 777 *
RUN chmod 777 -R /app
EXPOSE 5000 
CMD ["python3", "app.py"]
