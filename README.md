# STSen / Broadcast

> **お知らせ**
> 2026年5月22日、動作する見込みの状態まで更新しました。
> 尚、ソフトウェアの動作はライセンスに基づき保証されません。
> 利用の際は自己責任でお願いします
<!-- # Short Description -->

「同人音楽のトレンドをほぼ最速でキャッチできる生放送」

STSenで使用されている、自動放送システムのソースコードです。

[komatti365](https://github.com/komatti365)がフォークの上、[amanorox](https://github.com/amanorox)様のnicolivehelperの機能を一部組み込んで作成しました。

次の機能が実装されています

12時間放送への対応

Discordへの動画情報通知機能

運営コメントによる動画情報通知機能

環境変数による自動枠取りのon/off

キュー機能の追加

Dockerでの起動方法の追加

ニコニコログイン処理の変更

ログイン情報は平文で保存されています！暗号化は行っていないので情報の管理にご注意ください！！



このソフトウェアにはAIにより生成されたコードが含まれています

<!-- # Badges -->

[![Codacy Badge](https://app.codacy.com/project/badge/Grade/f26d74df081e4aa4ac231f1d149c5619)](https://www.codacy.com/gh/nucosen/broadcast/dashboard?utm_source=github.com&amp;utm_medium=referral&amp;utm_content=nucosen/broadcast&amp;utm_campaign=Badge_Grade)
[![Github license](https://img.shields.io/github/license/nucosen/broadcast)](https://github.com/nucosen/broadcast/blob/main/LICENSE)
[![GitHub Pipenv locked Python version](https://img.shields.io/github/pipenv/locked/python-version/nucosen/broadcast)](https://github.com/nucosen/broadcast/blob/main/Pipfile)
[![GitHub Release Date](https://img.shields.io/github/release-date/nucosen/broadcast)](https://github.com/nucosen/broadcast/releases/latest)
[![Website](https://img.shields.io/website?down_color=red&down_message=offline&up_color=success&up_message=online&url=https%3A%2F%2Fwww.nucosen.live%2F)](https://www.nucosen.live/)

## Tags

`niconico` `nicolive` `nico-nico-douga`

## Advantages

-   自動枠取り
-   REST APIを備えたデータベースでリクエスト受付ができる
-   枠の終了時間を確認し、音楽が途切れそうならば次枠に繰り越す

## Installation

Pipを用いてリリースのtar.gzファイルを展開してください。

Python3.10以降必須

代わりにブラウザのクッキーから抽出した `user_session` を使って認証することもできます。環境変数 `NICO_TOKEN` に `user_session` の値を設定すると、ユーザー名/パスワードの代わりにそのトークンでログインします。

### Docker

1. Docker イメージをビルドします。

```bash
docker build -t nucosen-broadcast .
```

2. 環境変数を `.env` に用意します。

3. コンテナを起動します。

```bash
docker compose up -d
```

必要に応じて `docker-compose.yml` の `NICO_COOKIE_FILE` のマウント先を調整してください。

## Minimal Example

`nucosen`コマンドで起動します

## Contributors

-   [sitting-cat](https://github.com/sitting-cat)
-   [komatti365](https://github.com/komatti365)

## LICENSE

See also [LICENSE file](https://github.com/nucosen/broadcast/blob/main/LICENSE).

Copyright 2022 NUCOSen運営会議

[![AGPLv3](https://github.com/nucosen/broadcast/blob/main/docs/AGPLv3.png)](https://github.com/nucosen/broadcast/blob/main/LICENSE)

<!-- CREATED_BY_LEADYOU_README_GENERATOR -->
