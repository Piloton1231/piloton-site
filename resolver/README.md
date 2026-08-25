# Piloton VRChat YouTube Resolver

`vrchat-youtube.html` が作成するURLを受け取り、公開YouTube動画の互換ストリームへHTTP 307で転送する小さなバックエンドです。動画本体は中継しません。

## 構成

- `GET /health` — 稼働確認
- `GET /resolve?url=<YouTube URL>` — YouTube URLを検証して一時的な `googlevideo.com` URLへ転送
- YouTubeのHTTPS動画URLだけを許可
- シェルを使わずyt-dlpを実行
- VRChatと相性のよい映像・音声一体型MP4を優先
- 簡易キャッシュ、同時実行数制限、アクセス回数制限付き
- 通常のアクセスログは無効

## 設置

1. 固定グローバルIPを持つ小型Linuxサーバーを用意し、DockerとDocker Composeをインストールします。
2. PorkbunのDNSで `video.piloton.cc` のAレコードをサーバーのIPv4アドレスへ向けます。
3. この `resolver` ディレクトリをサーバーへ配置します。
4. 必要なら `.env.example` を `.env` にコピーしてドメインを変更します。
5. `docker compose up -d --build` を実行します。
6. `https://video.piloton.cc/health` が `{"status":"ok"}` を返すことを確認します。

CaddyがHTTPS証明書を自動取得するため、サーバーのTCP 80番・443番ポートを公開する必要があります。サーバー管理画面やSSHは一般公開せず、OSとコンテナを定期的に更新してください。

## 更新

YouTube側の変更で抽出が動かなくなることがあります。`requirements.txt` のyt-dlpを公式の新しい安定版へ更新し、再ビルドしてください。バージョンを無確認で自動更新する構成にはしないでください。

## 制限と注意

- 非公開、メンバー限定、有料、年齢制限コンテンツには対応しません。
- GoogleアカウントのCookieや認証情報を入れないでください。
- VRChatで入口ドメインが信頼済みでない場合、「Allow Untrusted URLs」が必要です。
- 公開運用前に、利用規約・著作権、サーバー事業者の規約を確認してください。
- 大規模公開する場合は、アプリ内の簡易制限に加えてファイアウォールやCDN側のレート制限も設定してください。
