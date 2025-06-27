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