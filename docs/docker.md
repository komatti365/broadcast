# Docker での起動

NUCOSen Broadcast を Docker コンテナで動かすための手順です。

## 1. 必要なファイル

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `.env`

`Dockerfile` と `docker-compose.yml` はリポジトリルートに追加済みです。

## 2. .env ファイルの用意

ルートにある `.env.example` をコピーして `.env` を作成し、必要な値を設定してください。

```bash
copy .env.example .env
```

特に次の環境変数を設定してください。

- `LIVE_TITLE`
- `COMMUNITY`
- `TAGS`
- `REQTAGS`
- `NICO_ID`
- `NICO_PW` または `NICO_TOKEN`
- `LOGGING_DISCORD_WEBHOOK`
- `QUEUE_URL`
- `REQUEST_URL`
- `DB_KEY`
- `NG_TAGS`

## 3. コンテナビルド

```bash
docker build -t nucosen-broadcast .
```

または `docker compose up` 実行時に自動ビルドされます。

## 4. コンテナ起動

```bash
docker compose up -d
```

`docker-compose.yml` では次の設定を行っています。

- `.env` を読み込む
- `broadcast_data` というDockerのNamed Volume（名前付きボリューム）をコンテナ内の `/data` にマウントする
- `NICO_COOKIE_FILE=/data/nico_cookie.json` を環境変数として設定する
- 起動コマンドは `nucosen`

## 5. 永続化とログイン情報

`NICO_COOKIE_FILE` を `/data/nico_cookie.json` に設定しており、この `/data` はDockerのNamed Volumeとして管理されます。これによりホスト側（`./data`）に直接クッキーファイルが露出することなく、安全に永続化できます。

Named Volumeは自動作成されます。

## 6. コンテナ管理

- 起動: `docker compose up -d`
- 停止: `docker compose down`
- ログ: `docker compose logs -f`

## 7. 注意点

- 本リポジトリのソフトウェアは本番利用非推奨とされています。
- `NICO_TOKEN` を使う場合、ブラウザから取得した `user_session` を設定してください。
- `.env` に機密情報を含むため、公開しないようご注意ください。
