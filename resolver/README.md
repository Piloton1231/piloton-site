# Piloton Video URL Resolver

`vrchat-youtube.html` の公開YouTube動画URL転送と、`redgifs-original.html` の公開RedGifs MP4 URL取得を担当する小さなバックエンドです。動画本体は中継しません。

## DockHostingで無料運用

GitHubリポジトリ `Piloton1231/piloton-site` を選択し、Runtimeを `Dockerfile`、Root Directoryを `resolver`、App Portを `8000` にします。データベースは不要です。

Docker版は同梱のPOトークンサーバーとyt-dlpを使い、`/resolve` から映像・音声一体型MP4の一時的な `googlevideo.com` URLへ直接転送します。`PORT` はDockHostingから自動設定されます。

## Vercelで無料運用

個人利用はVercelのHobbyプランを利用できます。VercelへGitHubでログインして `Piloton1231/piloton-site` をImportし、Root Directoryを `resolver` にしてデプロイします。FastAPIは自動検出され、`vercel.json` によりシンガポールで実行されます。

デプロイ完了後、VercelのProject Settings → Domainsへ `video.piloton.cc` を追加します。表示されたCNAMEの宛先をPorkbun DNSへ登録し、`https://video.piloton.cc/health` が `{"status":"ok"}` を返せば完了です。

## 構成

- `GET /health` — 稼働確認
- `GET /resolve?url=<YouTube URL>` — YouTube URLを検証して一時的な `googlevideo.com` URLへ転送
- `GET /youtube/info?url=<YouTube URL>` — 動画タイトルまたはプレイリスト一覧を取得し、URLで指定された再生位置を先頭にしてJSONで返却
- `GET /redgifs/resolve?url=<RedGifs URL>` — 公開RedGifs URLを検証してHD優先のMP4 URLをJSONで返却
- YouTubeのHTTPS動画URLだけを許可
- RedGifsは公式サイトのHTTPS視聴・埋め込みURLだけを許可し、公式APIへ問い合わせ
- シェルを使わずyt-dlpを実行
- VRChatと相性のよい映像・音声一体型MP4を優先
- Vercel版でYouTube側に自前抽出を拒否された場合はKsync予備経路へ自動切替
- プレイリストは最大200件（`PLAYLIST_MAX_ITEMS` で変更可能）
- 簡易キャッシュ、同時実行数制限、アクセス回数制限付き
- 通常のアクセスログは無効

## 設置

1. 固定グローバルIPを持つ小型Linuxサーバーを用意し、DockerとDocker Composeをインストールします。
2. PorkbunのDNSで `video.piloton.cc` のAレコードをサーバーのIPv4アドレスへ向けます。
3. この `resolver` ディレクトリをサーバーへ配置します。
4. 必要なら `.env.example` を `.env` にコピーしてドメインを変更します。
5. `docker compose up -d --build` を実行します。
6. `https://video.piloton.cc/health` が `{"status":"ok"}` を返すことを確認します。

Vercelを利用する場合、上記のVPS向け手順は不要です。

CaddyがHTTPS証明書を自動取得するため、サーバーのTCP 80番・443番ポートを公開する必要があります。サーバー管理画面やSSHは一般公開せず、OSとコンテナを定期的に更新してください。

## 更新

YouTube側の変更で抽出が動かなくなることがあります。`requirements.txt` のyt-dlpを公式の新しい安定版へ更新し、再ビルドしてください。バージョンを無確認で自動更新する構成にはしないでください。

## 制限と注意

- 非公開、メンバー限定、有料、年齢制限コンテンツには対応しません。
- GoogleアカウントのCookieや認証情報を入れないでください。
- Vercelなど共有クラウドのIPはYouTubeに拒否される場合があります。その場合、無料構成ではKsync予備経路を利用します。
- VRChatで入口ドメインが信頼済みでない場合、「Allow Untrusted URLs」が必要です。
- 公開運用前に、利用規約・著作権、サーバー事業者の規約を確認してください。
- 大規模公開する場合は、アプリ内の簡易制限に加えてファイアウォールやCDN側のレート制限も設定してください。
