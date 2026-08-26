# Piloton Direct Resolver

Vercel の Node.js Function で YouTube の公開動画を、映像・音声一体型の一時的な `googlevideo.com` MP4 URLへ307転送します。動画本体は中継しません。

Vercelで同じGitHubリポジトリを新しいプロジェクトとしてImportし、Root Directoryを `direct-resolver` に設定します。デプロイ後は次を確認します。

- `GET /health`
- `GET /resolve?url=<HTTPS YouTube URL>`

`/resolve` が返す `Location` は `.googlevideo.com` に限定しています。非公開、ログイン必須、年齢制限付き、有料動画は対象外です。
