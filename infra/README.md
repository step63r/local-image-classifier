# AWSデプロイ手順

EC2上の自前PostgreSQL(+pgvector拡張のみ有効化、タグ検索スコープ)+ S3 + CloudFrontで
このアプリをホストするための手順。詳細な設計判断は `.claude/plans/hashed-orbiting-babbage.md` を参照。

インスタンスにはSSH鍵も22番ポートも一切無い。管理アクセスはAWS Systems Manager
Session Manager経由のみ(AWSコンソールのEC2画面から「接続」→「セッションマネージャー」で
ブラウザだけでシェルに入れる。CLIから使いたい場合は `aws ssm start-session --target <instance-id>`)。

## 事前準備

```powershell
cd infra
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

ローカルの`aws ssm start-session`やSSMポートフォワード(データ移行時に使用)には
Session Manager Pluginが必要(インストール済み: `session-manager-plugin --version`で確認可)。

ドメインのDNSはCloudflareで管理する想定(Route53ホストゾーンはこのアカウントに無い)。
ドメインは `image-classifier.minatoproject.com` を使用。

## 1. ACM証明書(us-east-1)

```powershell
cd infra
$env:PATH += ";$PWD\.venv\Scripts"
cdk deploy ImageClassifierCertStack -c domainName=image-classifier.minatoproject.com
```

DNS検証待ちで`CREATE_IN_PROGRESS`のまま止まる。別ターミナルで検証用CNAMEを取得:

```powershell
aws acm list-certificates --region us-east-1
aws acm describe-certificate --region us-east-1 --certificate-arn <arn> `
  --query "Certificate.DomainValidationOptions"
```

**手動作業**: 出力された`_xxxx.image-classifier.minatoproject.com CNAME _yyyy.acm-validations.aws.`を
Cloudflareのminatoproject.comのDNS管理画面で追加する。
**プロキシは必ず「DNSのみ(グレークラウド)」にすること**(オレンジクラウドでプロキシしない)。

検証が完了すると上記`cdk deploy`コマンドが自動的に完了する。

## 2. アプリ本体(ap-northeast-1)

```powershell
cdk deploy ImageClassifierAppStack -c domainName=image-classifier.minatoproject.com `
  -c authUsername=<好きなユーザー名> -c authPassword=<好きなパスワード>
```

完了後、出力される`InstanceId`・`InstancePublicIp`・`MediaBucketName`・`DistributionDomainName`を控える。

## 3. インスタンスの初期セットアップ確認

UserDataで`system_setup.sh`が自動実行されるが、AL2023の`postgresql16`パッケージ名は環境依存の可能性があるため、
一度Session Managerで確認する。AWSコンソールのEC2 → 対象インスタンス → 「接続」→「セッションマネージャー」
タブから「接続」を押すだけでブラウザ上のシェルに入れる。CLIからなら:

```powershell
aws ssm start-session --target <InstanceId>
```

シェルに入ったら:

```bash
sudo tail -f /var/log/cloud-init-output.log   # 初回実行ログ
sudo systemctl status postgresql
sudo -u postgres psql -d imagedb -c "\dt"
sudo -u postgres psql -d imagedb -c "SELECT extname FROM pg_extension;"   # vector が入っているか
```

問題があれば`/opt/system_setup.sh`を修正して再実行してよい(冪等スクリプト)。

## 4. データ移行

ローカルのメインvenv(リポジトリ直下の`.venv`)を使う。PostgreSQLは`localhost`のみ待受なので、
SSHの代わりにSSMのポートフォワードでトンネルを張る:

```powershell
aws ssm start-session --target <InstanceId> `
  --document-name AWS-StartPortForwardingSession `
  --parameters '{"portNumber":["5432"],"localPortNumber":["5433"]}'
```

別ターミナルで、まずスモークテスト:

```powershell
$env:PGPASSWORD = "<インスタンス上の /opt/imageapp/app.env の DATABASE_URL から確認>"
python migrate_to_aws.py --s3-bucket <MediaBucketName> --limit 20
```

問題なければ全件実行(約44,242件・24GB、時間がかかる):

```powershell
python migrate_to_aws.py --s3-bucket <MediaBucketName>
```

中断しても同じコマンドで再開可能。

## 5. アプリのデプロイ

SSH/scpは使わず、S3経由でコードを配ってSSM Run Commandでサービスを再起動する:

```powershell
$env:IMAGEAPP_INSTANCE_ID = "<InstanceId>"
$env:IMAGEAPP_BUCKET = "<MediaBucketName>"
.\deploy_app.ps1
```

## 6. 動作確認

```powershell
# インスタンス上で直接(Session Manager経由)
aws ssm start-session --target <InstanceId>
# シェル内で: curl -u <authUsername>:<authPassword> http://localhost:8000/

# CloudFrontのデフォルトドメイン経由(カスタムドメインのDNS切り替え前でも動く)
curl -u <authUsername>:<authPassword> https://<DistributionDomainName>/
```

## 7. 独自ドメインへの切り替え

**手動作業**: Cloudflareで本番CNAMEを追加: `image-classifier.minatoproject.com CNAME <DistributionDomainName>`。
こちらも「DNSのみ」(オレンジクラウド禁止 — 二重CDN+証明書不整合になる)。

```powershell
curl -u <authUsername>:<authPassword> https://image-classifier.minatoproject.com/
```

## 以後のコード変更

アプリコード(`app.py`/`templates/`)を変更したら `.\deploy_app.ps1` を再実行するだけでよい。
インフラ変更は `infra` ディレクトリで `cdk diff` → `cdk deploy`(常に同じ `-c domainName=...` を付ける)。

## 既知の注意点

- **SSHは完全に廃止**: セキュリティグループに22番ポートは無く、キーペアも作成しない。
  管理アクセスはSession Manager(AWSコンソールまたは`aws ssm start-session`)のみ。
  ポートフォワードもSSMの`AWS-StartPortForwardingSession`ドキュメントで代替している。
- **CloudFrontのキャッシュと認証**: `/image/*`・`/thumb/*`のキャッシュキーには`Authorization`ヘッダーが含まれる。
  同じ認証情報でのリクエストはキャッシュヒットするが、認証情報が異なる/無い場合は必ずオリジン(Flask)に転送され401になる。
  個人利用前提での許容範囲のトレードオフとして受け入れている。
- **S3バケットは`RemovalPolicy.RETAIN`**: `cdk destroy`しても消えない。24GBの原本を誤って失わないため。
  本当に削除したい場合は手動で`aws s3 rm --recursive`してからバケット削除する。
