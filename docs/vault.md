# HashiCorp Vault / OpenBao 連携ガイド

NUCOSen Broadcast は、パスワードやトークンなどの機密情報をファイルから読み込む機能（`_FILE` 環境変数）をサポートしています。
この機能と **Vault Agent Template** を組み合わせることで、アプリケーションのコードを変更することなく、HashiCorp Vault や OpenBao から安全に機密情報を注入できます。

## Vault Agent を使った連携の仕組み

1. Vault Agent コンテナが、Vaultサーバーと通信してシークレット（KVストア等）を取得します。
2. Vault Agent は、取得したシークレットを指定されたフォーマットのテキストファイル（例: `/secrets/nico_token.txt`）として出力します。
3. NUCOSen Broadcast コンテナは、そのファイルが配置されたディレクトリを共有ボリューム（Shared Volume）としてマウントし、ファイルから機密情報を読み込みます。

## 設定例

### 1. Vault Agent テンプレート (`secrets.ctmpl`)

Vault Agent に出力させるファイルのテンプレートを作成します。
この例では、Vaultの `secret/data/nucosen` というパスに機密情報が保存されていると仮定します。

```text
{{ with secret "secret/data/nucosen" }}
{{ .Data.data.nico_token }}
{{ end }}
```

このファイルを `./vault/secrets.ctmpl` などとして保存します。
必要に応じて、`nico_pw` や `db_key` 用のテンプレートも同様に作成します。

### 2. Vault Agent 設定ファイル (`vault-agent.hcl`)

Vault Agent の動作を設定します。

```hcl
pid_file = "/tmp/pidfile"

vault {
  address = "http://vault:8200"
}

auto_auth {
  method {
    type = "approle"
    config = {
      role_id_file_path   = "/vault/config/role_id"
      secret_id_file_path = "/vault/config/secret_id"
    }
  }
}

template {
  source      = "/vault/config/secrets.ctmpl"
  destination = "/secrets/nico_token.txt"
}
```

このファイルを `./vault/vault-agent.hcl` として保存します。

### 3. Docker Compose 構成 (`docker-compose.yml`)

Vault Agent コンテナと NUCOSen Broadcast コンテナを連携させ、`/secrets` ボリュームを共有します。

```yaml
version: "3.9"

services:
  broadcast:
    image: nucosen-broadcast:latest
    restart: unless-stopped
    environment:
      # _FILE を使って、共有ボリュームに出力されたファイルを指定
      - NICO_TOKEN_FILE=/secrets/nico_token.txt
      - DB_KEY_FILE=/secrets/db_key.txt
      # その他必要な環境変数...
    volumes:
      - shared_secrets:/secrets:ro # 読み取り専用でマウント
    depends_on:
      - vault-agent

  vault-agent:
    image: hashicorp/vault:latest
    command: agent -config=/vault/config/vault-agent.hcl
    volumes:
      - ./vault:/vault/config:ro
      - shared_secrets:/secrets
    environment:
      - VAULT_ADDR=http://vault-server:8200

volumes:
  shared_secrets:
    driver: local
```

## メリット
- Portainer等の環境変数一覧には `/secrets/...` というパスしか表示されません。
- コンテナの中やホストOSの永続ディスクに機密情報が残りません（インメモリボリュームを使うとより安全です）。
- トークンが更新された場合でも、Vault Agent がファイルを自動更新すれば、アプリ再起動時に新しい情報が読み込まれます。
