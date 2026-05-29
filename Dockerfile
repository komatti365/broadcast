FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 依存関係定義ファイルを先にコピーしてインストールし、キャッシュ効率を最大化します
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# 残りのソースコードをコピーしてインストールします
COPY . /app
RUN pip install --no-cache-dir --no-deps .

CMD ["nucosen"]
