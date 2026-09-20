# local-image-classifier

## Description

外付けHDDに溜まった大量の画像をタグ検索できるようにする、個人用の画像分類・検索ツールです。

ローカルでWD14(ONNX)モデルにより画像へタグ付けしてSQLiteへ保存し、その結果をAWS(EC2上の自前PostgreSQL + S3 + CloudFront)へ移行して、Flask製の検索UIからタグ検索・フォルダ絞り込み・詳細表示を行えます。

## Requirement

- Python 3.13
- [uv](https://docs.astral.sh/uv/)（venv管理）
- CUDA / cuDNN（GPUでタグ付けする場合。パスの通し方など詳細は後述）
- AWS CLI + [Session Manager Plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)（AWSへのデプロイ・データ移行を行う場合）
- AWS CDK（`infra`ディレクトリ経由で利用、グローバルインストール不要）

## Install

```powershell
git clone git@github.com:step63r/local-image-classifier.git
cd local-image-classifier
uv venv .venv
uv pip install -r requirements.txt
```

AWSインフラをデプロイする場合は、追加で`infra`用の仮想環境も作成します。

```powershell
cd infra
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Usage

### ローカルでのタグ付け

画像フォルダを指定してWD14タグ付けを実行し、結果を`tags.db`(SQLite)に保存します。同じコマンドを再実行すると、既にタグ付け済み(パス・サイズ・更新日時・モデルが一致)のファイルはスキップされるため、そのまま差分更新として使えます。

```powershell
python batch_tag.py --dir <画像フォルダ> --recursive
```

主なオプション:

- `--model`: 使用するWD14モデル(デフォルト`wd-eva02-large-tagger-v3`)
- `--general-threshold` / `--character-threshold`: DB保存時の足切り閾値(デフォルト0.1、表示・検索時の閾値は別途`app.py`側で調整可能)
- `--force`: 既にタグ付け済みでも再処理する
- `--limit`: 動作確認用に処理件数を制限する

GPU推論にはCUDA / cuDNNのDLLがPATHに通っている必要があります。通っていない場合は黙ってCPU実行にフォールバックするため、`tagger.session.get_providers()`のログ出力(`Providers in use: ...`)で`CUDAExecutionProvider`が使われているか確認してください。

### ローカルでの検索UI動作確認

`app.py`はPostgreSQL(`DATABASE_URL`)とS3(`S3_BUCKET`)から読み取る構成になっており、ローカルのSQLiteは直接参照しません。手元で動作確認する場合は、Docker等でPostgreSQL(pgvector拡張入り)を用意するか、後述のSSMポートフォワードで本番DBに接続してください。

```powershell
$env:DATABASE_URL = "postgresql://<user>:<password>@<host>:<port>/<dbname>"
$env:S3_BUCKET = "<バケット名>"
$env:AUTH_USERNAME = "<任意のユーザー名>"
$env:AUTH_PASSWORD = "<任意のパスワード>"
python app.py
```

`AUTH_USERNAME` / `AUTH_PASSWORD`未設定の場合はデフォルト(`admin` / `changeme`)で起動しつつ警告が出ます。ローカル確認以外では必ず設定してください。

### AWSへの初回デプロイ

CDKスタックのデプロイ、EC2の初期セットアップ確認、データ移行、アプリデプロイ、独自ドメイン切り替えまでの手順は[infra/README.md](infra/README.md)にまとめています。EC2にはSSH鍵も22番ポートも無く、管理アクセスはAWS Systems Manager Session Managerのみで行います。

### 差分更新の運用(ローカルで画像が増えた場合)

新しい画像を追加したら、以下の2コマンドを手動で順に実行します(自動実行のスケジューリングは行わない方針)。どちらも再開可能・差分スキップ設計のため、同じコマンドをそのまま再実行するだけで新規分のみが処理されます。`migrate_to_aws.py`は既存分の判定を一括取得(1クエリ)で行うため、新規画像が無くてもスキップ判定自体は数秒で終わります(実際の処理時間は新規追加分のアップロード量に応じて変わります)。

```powershell
# 1. 新規/更新分だけWD14タグ付け -> tags.db
python batch_tag.py --dir <画像フォルダ> --recursive

# 2. SSMポートフォワードでPostgreSQLへトンネル(別ターミナル)
aws ssm start-session --target <InstanceId> `
  --document-name AWS-StartPortForwardingSession `
  --parameters '{"portNumber":["5432"],"localPortNumber":["5433"]}'

# 3. 新規/未移行分だけS3 + Postgresへ移行
$env:PGPASSWORD = "<EC2上のapp.envのDATABASE_URLから確認>"
python migrate_to_aws.py --s3-bucket <MediaBucketName>
```

**注意**: このフローは追加・更新のみを検出します。ローカルで削除・リネームされたファイルのレコードはSQLite/Postgres双方に残り続けますが、画像フォルダは読み取り専用として運用しているため、当面は許容する方針としています。

### コード変更時のデプロイ

`app.py` / `templates/`を変更した場合は、S3経由でコードを配ってSSM Run Commandでサービスを再起動するだけで反映できます。

```powershell
$env:IMAGEAPP_INSTANCE_ID = "<InstanceId>"
$env:IMAGEAPP_BUCKET = "<MediaBucketName>"
.\deploy_app.ps1
```

インフラ変更は`infra`ディレクトリで`cdk diff` → `cdk deploy`を実行します(常に同じ`-c domainName=...`を付ける、詳細は[infra/README.md](infra/README.md)参照)。

## Contribution

個人利用のプロジェクトのため、現時点ではコントリビューションは受け付けていません。

## Author

[minato](https://www.minatoproject.com/)
