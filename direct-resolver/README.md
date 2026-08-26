# Piloton Direct Resolver

Node.jsでYouTubeの公開動画を、映像・音声一体型の一時的な `googlevideo.com` MP4 URLへ307転送します。動画本体は中継しません。Vercel Functionと通常のDockerサービスの両方で起動できます。

Vercelで同じGitHubリポジトリを新しいプロジェクトとしてImportし、Root Directoryを `direct-resolver` に設定します。デプロイ後は次を確認します。

Docker対応サービスではRoot Directoryを `direct-resolver`、Dockerfileを `Dockerfile`、公開ポートを `8000` に設定します。

- `GET /health`
- `GET /resolve?url=<HTTPS YouTube URL>`

`/resolve` が返す `Location` は `.googlevideo.com` に限定しています。非公開、ログイン必須、年齢制限付き、有料動画は対象外です。

YouTubeへの通信を固定ISPプロキシ経由にする場合は、VercelのEnvironment Variablesへ `YOUTUBE_PROXY_URL` を追加します。値は `http://USER:PASS@HOST:PORT` 形式で登録し、GitHubには保存しません。`/health` の `proxyEnabled` が `true` なら設定済みです。
