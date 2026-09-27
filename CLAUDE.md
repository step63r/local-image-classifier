# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

外付けHDDに溜まった大量の画像をタグ検索できるようにする個人用ツール。ローカルでWD14(ONNX)モデルにより画像へタグ付けしてSQLiteへ保存し、その結果をAWS(EC2上の自前PostgreSQL + S3 + CloudFront)へ移行して、Flask製の検索UIからタグ検索・フォルダ絞り込み・詳細表示・類似画像表示を行う。個人利用のみを想定した設計判断(認証方式、SSH廃止、未インデックスDB等)が随所にある。

## Commands

セットアップ(uv):

```powershell
uv venv .venv
uv pip install -r requirements.txt
```

ローカルタグ付け(差分スキップ・再開可能。同一パス・サイズ・mtime・モデルなら再実行してもスキップされる):

```powershell
python batch_tag.py --dir <画像フォルダ> --recursive
```

検索UIをローカルで動作確認(PostgreSQL/S3必須。SQLiteは参照しない):

```powershell
$env:DATABASE_URL = "postgresql://<user>:<password>@<host>:<port>/<dbname>"
$env:S3_BUCKET = "<バケット名>"
$env:AUTH_USERNAME = "<任意のユーザー名>"
$env:AUTH_PASSWORD = "<任意のパスワード>"
$env:SECRET_KEY = "<セッションCookie署名用のランダム文字列>"
$env:SECURE_COOKIES = "0"  # ローカルのhttp://で確認する場合のみ
python app.py
```

差分更新(新規画像追加時、この2コマンドを手動で順に実行。自動スケジューリングはしない方針):

```powershell
python batch_tag.py --dir <画像フォルダ> --recursive
# 別ターミナルでSSMポートフォワードを張った上で
python migrate_to_aws.py --s3-bucket <MediaBucketName>
```

アプリコード(`app.py`/`templates/`)のデプロイ(S3経由でSSM Run Command、SSH不使用):

```powershell
$env:IMAGEAPP_INSTANCE_ID = "<InstanceId>"
$env:IMAGEAPP_BUCKET = "<MediaBucketName>"
.\deploy_app.ps1
```

インフラ変更(`infra`ディレクトリ、専用venv):

```powershell
cd infra
cdk diff -c domainName=image-classifier.minatoproject.com
cdk deploy -c domainName=image-classifier.minatoproject.com
```

テスト・Lint・型チェックの自動化は現状このリポジトリに存在しない(pytest/ruff等の設定なし)。手動確認は`--limit N`オプションで少数件のスモークテストを行うのが基本パターン(`batch_tag.py --limit`, `migrate_to_aws.py --limit`, `backfill_embeddings.py --limit`, `push_embeddings.py --limit`)。

AWS初回デプロイの全手順・既知の注意点は[infra/README.md](infra/README.md)を参照。

## Architecture

### 2段階パイプラインとローカル/サーバの依存分離

1. **ローカル(タグ付け)**: `tagger/`(WD14 ONNX推論とSQLiteスキーマ)+ `batch_tag.py` が画像フォルダを`tags.db`(SQLite)へ変換する。GPU推論はCUDA/cuDNNのDLLがPATHに無いと黙ってCPUへフォールバックするため、`Providers in use:`ログで`CUDAExecutionProvider`の使用を確認する必要がある。
2. **移行**: `migrate_to_aws.py`が`tags.db`を読み、原本画像とサムネイルをS3へ、メタデータをPostgres(pgvector)へコピーする。`push_embeddings.py`/`backfill_embeddings.py`はembedding列を後付けするための一度きりの移行スクリプト。
3. **サーバ(検索UI)**: `app.py`はPostgreSQL(`DATABASE_URL`)とS3(`S3_BUCKET`)のみを参照し、SQLiteには一切触れない。`requirements-server.txt`は`requirements.txt`から意図的にonnxruntime/opencv/huggingface_hub/tqdm/numpy/Pillowを除いた最小構成で、EC2にはこちらだけをデプロイする。

`batch_tag.py`と`migrate_to_aws.py`はいずれも「同じコマンドをそのまま再実行すれば新規/未処理分だけ処理される」設計(冪等・再開可能)。ローカルでの削除・リネームは検出/反映されない(読み取り専用運用を前提に許容)。

### tagger/ の責務分割

- `tagger/models.py`: `WD14Tagger`(ONNXモデルのロード・前処理・推論)。`MODEL_REGISTRY`でHFの短縮名→リポジトリIDを解決。`EMBEDDING_TAPS`は類似画像検索用embeddingを取れるモデル(現状EVA02のみ)の内部テンソル名を保持し、対応する`infer()`/`embed()`が同じforward passでタグ確率とembeddingを両方(または片方だけ)取得する。
- `tagger/database.py`: SQLiteスキーマ(`images`/`tags`/`embeddings`)と保存関数。`already_done()`がパス・サイズ・mtime・モデル一致で差分スキップを判定する。

### しきい値の二段構え

`batch_tag.py`は`--general-threshold`/`--character-threshold`(デフォルト0.1)という低い足切りでDBに保存し、実際の表示/検索用しきい値(`DEFAULT_GENERAL_MIN=0.3`, `DEFAULT_CHARACTER_MIN=0.85`)は`app.py`のクエリ時に適用される。これにより、しきい値のチューニングに再タグ付けが不要になっている。

### app.py: 認証・検索・配信

- `login_required`デコレータ + Flaskセッションによる単一ユーザー認証。画像/サムネイルの配信(`/image/<id>`, `/thumb/<id>`)もこのセッションが唯一のゲートで、CloudFrontのキャッシュキーに`imgapp_session`が含まれる(infra/README.md参照、個人利用前提のトレードオフ)。
- `parse_query()`がタグ検索クエリを解析し、`"引用符"`は完全一致、それ以外は部分一致(LIKE)として扱う。アンダースコアはDB格納形式(スペース)に正規化される。
- `search_images()`はタグ・フォルダ・カテゴリ別しきい値・センシティブレーティング除外を動的にSQL条件へ組み立てる。
- `get_related_images()`はpgvectorのコサイン距離(`<=>`)でembeddingを持つ画像同士の類似検索を行う(embeddingが無い画像では呼び出されない)。
- htmx(`HX-Request`ヘッダ)によりページ全体とグリッド部分(`_grid.html`)のレンダリングを切り替えている。

### infra/ (AWS CDK)

`infra/stacks/`に3スタック: `certificate_stack.py`(us-east-1のACM証明書)、`app_stack.py`(EC2 t4g.micro + 自前PostgreSQL/pgvector + S3 + CloudFront)、`waf_stack.py`。EC2にはSSH鍵も22番ポートも無く、管理アクセスはSystems Manager Session Managerのみ(ポートフォワードも`AWS-StartPortForwardingSession`で代替)。S3バケットは`RemovalPolicy.RETAIN`で`cdk destroy`しても消えない。ドメイン(`image-classifier.minatoproject.com`)のDNSはCloudflareで管理し、CNAMEは必ず「DNSのみ」(プロキシ無効)にする必要がある。
