# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

外付けHDDに溜まった大量の画像・動画・GIFをタグ検索できるようにする個人用ツール(サービス名は「画像・動画タグ検索システム」)。ローカルでWD14(ONNX)モデルにより画像(動画はフレームを抽出して)へタグ付けしてSQLiteへ保存し、その結果をAWS(EC2上の自前PostgreSQL + S3 + CloudFront)へ移行して、Flask製の検索UIからタグ検索・フォルダ絞り込み・種別/再生時間絞り込み・詳細表示(動画はストリーミング再生)・類似表示を行う。個人利用のみを想定した設計判断(認証方式、SSH廃止、未インデックスDB等)が随所にある。

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

動画・GIFのタグ付け(同じ`tags.db`へ追記。対象は`.mp4 .mov .gif`で、`batch_tag.py`とは別コマンド。既定は10秒間隔・最大60フレーム/本で`--interval`/`--max-frames`で変更可):

```powershell
python batch_tag_video.py --dir <動画フォルダ> --recursive
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

動画を追加した場合は`batch_tag_video.py`で同様にタグ付けしたうえで、`migrate_to_aws.py`に`--media video`を付けて実行する(画像は`--media image`、省略時は両方)。動画は1本が最大GB級でアップロードに時間がかかるが、完了済みの本は再実行時にスキップされる(中断時は未完了の1本を最初から再送する)。

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

テスト・Lint・型チェックの自動化は現状このリポジトリに存在しない(pytest/ruff等の設定なし)。手動確認は`--limit N`オプションで少数件のスモークテストを行うのが基本パターン(`batch_tag.py --limit`, `batch_tag_video.py --limit`, `migrate_to_aws.py --limit`, `backfill_embeddings.py --limit`, `push_embeddings.py --limit`)。`migrate_to_aws.py`の`--limit`は`--media`による絞り込み**後**に適用されるため、移行済みの画像が多い環境でも`--media video --limit 5`で動画だけを試せる(`--media`なしの`--limit 1`はスキーマ反映だけの用途で、先頭の移行済み行をスキップして終わる)。

AWS初回デプロイの全手順・既知の注意点は[infra/README.md](infra/README.md)を参照。

## Architecture

### 2段階パイプラインとローカル/サーバの依存分離

1. **ローカル(タグ付け)**: `tagger/`(WD14 ONNX推論とSQLiteスキーマ)+ `batch_tag.py`(画像)/`batch_tag_video.py`(動画・GIF)が`tags.db`(SQLite)へ変換する。GPU推論はCUDA/cuDNNのDLLがPATHに無いと黙ってCPUへフォールバックするか、cuDNNのロード失敗で全件エラーになるため、`Providers in use:`ログで`CUDAExecutionProvider`の使用を確認する必要がある(動画も同様)。
2. **移行**: `migrate_to_aws.py`が`tags.db`を読み、原本(画像・動画)とサムネイルをS3へ、メタデータをPostgres(pgvector)へコピーする。動画のサムネイルはOpenCVで取り出した中間フレーム。`push_embeddings.py`/`backfill_embeddings.py`はembedding列を後付けするための一度きりの移行スクリプト。
3. **サーバ(検索UI)**: `app.py`はPostgreSQL(`DATABASE_URL`)とS3(`S3_BUCKET`)のみを参照し、SQLiteには一切触れない。`requirements-server.txt`は`requirements.txt`から意図的にonnxruntime/opencv/huggingface_hub/tqdm/numpy/Pillowを除いた最小構成で、EC2にはこちらだけをデプロイする。

`batch_tag.py`・`batch_tag_video.py`・`migrate_to_aws.py`はいずれも「同じコマンドをそのまま再実行すれば新規/未処理分だけ処理される」設計(冪等・再開可能)。ローカルでの削除・リネームは検出/反映されない(読み取り専用運用を前提に許容)。検索UIの詳細画面から削除した画像・動画はS3(original/thumb)とPostgresから消え、パスが`deleted_images`テーブルに記録されて`migrate_to_aws.py`の再実行でも復活しない(EC2ロールのS3削除権限は`app_stack.py`の`grant_delete`)。

### tagger/ の責務分割

- `tagger/models.py`: `WD14Tagger`(ONNXモデルのロード・前処理・推論)。`MODEL_REGISTRY`でHFの短縮名→リポジトリIDを解決。`EMBEDDING_TAPS`は類似画像検索用embeddingを取れるモデル(現状EVA02のみ)の内部テンソル名を保持し、対応する`infer()`/`embed()`が同じforward passでタグ確率とembeddingを両方(または片方だけ)取得する。
- `tagger/database.py`: SQLiteスキーマ(`images`/`tags`/`embeddings`)と保存関数。`already_done()`がパス・サイズ・mtime・モデル一致で差分スキップを判定する。動画も`images`の1行(`media_type='video'`+`duration_sec`/`width`/`height`)として同じテーブルに入れ、`tags`/`embeddings`/削除/関連表示を共用する。これらの列はSCHEMAではなく`_ensure_media_columns()`が`PRAGMA table_info`で有無を確認して`ALTER`する(SQLiteに`ADD COLUMN IF NOT EXISTS`が無く、既存の`tags.db`にも効かせるため)。PostgreSQL側の同等の追加は`migrate_to_aws.py`の`PG_SCHEMA`。
- `tagger/video.py`: 動画のメタ情報取得(OpenCV)、区間の中央点での等間隔フレーム抽出、フレーム推論結果の集約。タグはフレーム間のmax confidence、embeddingは平均。**ratingは「最大の1個」ではなく、閾値0.5以上で最も厳しいもの**を保存する(1フレームだけexplicitでも`exclude sensitive`をすり抜けさせないため)。再生時間はOpenCVのフレーム数/fpsからの算出なので可変フレームレートでは誤差が出うる。

### しきい値の二段構え

`batch_tag.py`は`--general-threshold`/`--character-threshold`(デフォルト0.1)という低い足切りでDBに保存し、実際の表示/検索用しきい値(`DEFAULT_GENERAL_MIN=0.3`, `DEFAULT_CHARACTER_MIN=0.85`)は`app.py`のクエリ時に適用される。これにより、しきい値のチューニングに再タグ付けが不要になっている。

### app.py: 認証・検索・配信

- `login_required`デコレータ + Flaskセッションによる単一ユーザー認証。画像/サムネイルの配信(`/image/<id>`, `/thumb/<id>`)もこのセッションが唯一のゲートで、CloudFrontのキャッシュキーに`imgapp_session`が含まれる(infra/README.md参照、個人利用前提のトレードオフ)。
- 動画の配信(`/video/<id>`)はEC2を経由させず、ログイン確認後にS3署名付きURL(有効1時間、`Cache-Control: no-store`)へ302する。ブラウザがS3から直接Rangeで取得する(CloudFrontもEC2も通らないため、転送量はS3の無料枠が対象)。署名付きURLは期限内なら誰でも使えるベアラーリンクになる点は個人利用前提で許容。署名用クライアント`s3_presign`はリージョン・SigV4に加え`addressing_style: virtual`を明示している(boto3 1.35.99はリージョン指定だけだとURLのホストがグローバル`bucket.s3.amazonaws.com`になる)。GIFは`<video>`で再生できないため、種別は動画でも詳細画面では`/image/<id>`の`<img>`で表示する(`play_as_video`)。
- `parse_query()`がタグ検索クエリを解析し、`"引用符"`は完全一致、それ以外は部分一致(LIKE)として扱う。アンダースコアはDB格納形式(スペース)に正規化される。
- `search_images()`はタグ・フォルダ・カテゴリ別しきい値・センシティブレーティング除外・種別(`media`)・再生時間(`dur_min`/`dur_max`、UI上は分)を動的にSQL条件へ組み立てる。画像は`duration_sec`がNULLなので、再生時間を指定すると結果は自然に動画だけになる。再生時間の並び順(`duration_*`)は`NULLS LAST`。種別・再生時間はURL引数として`build_url()`(キーワード引数)や`_grid.html`の`filter_qs`で引き回しており、**保存検索(`saved_searches`)はこれらを保持できない**ため、指定中は★を出さない。
- `get_related_images()`はpgvectorのコサイン距離(`<=>`)でembeddingを持つ画像同士の類似検索を行う(embeddingが無い画像では呼び出されない)。画像と動画は同じembedding空間にあるが、関連表示は同じ`media_type`同士に限定する。
- 詳細画面のファイルパスリンクは、キーワード空・`folder`=親フォルダ(末尾`\`付き)・`sort=mtime_desc`の通常検索(`/`)へ飛ぶ。キーワードが空でもフォルダ指定または種別・再生時間の指定があれば検索として成立する(`is_searchable()`)。この場合は関連度ソートを計算できないため、`index()`が`FOLDER_ONLY_SORT`へ差し替える。`folder`はLIKE部分一致なのでサブフォルダ配下も含まれる。DBのパスはWindows形式(`\`区切り)だがEC2はLinuxなので、親フォルダ算出は`os.path`ではなく`ntpath`を使う。関連度以外の並び順は`COLUMN_SORTS`。
- htmx(`HX-Request`ヘッダ)によりページ全体とグリッド部分(`_grid.html`)のレンダリングを切り替えている。

### infra/ (AWS CDK)

`infra/stacks/`に3スタック: `certificate_stack.py`(us-east-1のACM証明書)、`app_stack.py`(EC2 t4g.micro + 自前PostgreSQL/pgvector + S3 + CloudFront)、`waf_stack.py`。EC2にはSSH鍵も22番ポートも無く、管理アクセスはSystems Manager Session Managerのみ(ポートフォワードも`AWS-StartPortForwardingSession`で代替)。S3バケットは`RemovalPolicy.RETAIN`で`cdk destroy`しても消えない。

**EC2再作成の二重チェック(必須)**: PostgreSQLのデータはEC2のルートEBS上にあり、バックアップも別ボリュームも無いため、`ec2.Instance`が置き換わる(Replacement)と全データが失われる(過去に実際に発生)。`user_data`・AMI・インスタンスタイプ・ブロックデバイス・参照するAssetなど、EC2の再作成を招きうる変更(依存関係経由の変更も含む)を`infra/`に加える場合、Claudeは`cdk deploy`を案内する前に必ずユーザへ`cdk diff <スタック名>`の出力をClaude Codeに貼り付けるよう要求し、`AppInstance`に`Replacement`/`replace`が含まれないかを確認してから可否を伝える。diff出力を見ずに「デプロイして安全」とは言わない。Replacementが含まれる場合は、デプロイを止めてDBのバックアップ手段を先に提案する。`infra/`を変更しない場合でも、`system_setup.sh`等のAsset変更はuser_dataに波及するため同様に扱う。ドメイン(`image-classifier.minatoproject.com`)のDNSはCloudflareで管理し、CNAMEは必ず「DNSのみ」(プロキシ無効)にする必要がある。
