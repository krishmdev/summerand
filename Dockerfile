###############  build  ################
FROM python:3.11-slim AS build
WORKDIR /app
COPY pyproject.toml .
RUN pip install --upgrade pip && pip install -e .

################  runtime  ################
FROM python:3.11-slim
WORKDIR /app
COPY --from=build /usr/local /usr/local
COPY summerand/ summerand/

# if folder = summerand/
COPY summerand/ summerand/
CMD ["python", "-m", "summerand"]

######## rss-ingest ########
FROM python:3.11-slim AS rss-ingest
WORKDIR /app
RUN pip install --no-cache-dir aiohttp feedparser aiokafka bloom-filter2
COPY summerand/ingest/rss_spider.py .
CMD ["python", "rss_spider.py"]


######## md-equity ########
FROM python:3.11-slim AS md-equity
WORKDIR /app
RUN pip install --no-cache-dir aiokafka websockets
COPY summerand/api/polygon_scraper.py .
CMD ["python", "polygon_scraper.py"]