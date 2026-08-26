# Piloton Video URL Resolver

`vrchat-youtube.html` の公開YouTube動画URL転送と、`redgifs-original.html` の公開RedGifs MP4 URL取得を担当する小さなバックエンドです。動画本体は中継しません。

## Renderで無料運用

`render.yaml` をBlueprintとして読み込むと、Docker版のWebサービスが作成されます。Docker版はYouTubeの証明トークン生成器を同じコンテナ内で起動し、映像・音声一体型の一時MP4 URLを取得します。

デプロイ完了後、Renderのサービスに `video.piloton.cc` を追加し、Renderに表示されたCNAMEの宛先をDNSへ登録します。`https://video.piloton.cc/health` が `{"status":"ok"}` を返せば完了です。

## 構成

- `GET /health` — 稼働確認
- `GET /resolve?url=<YouTube URL>` — YouTube URLを検証して一時的な `googlevideo.com` URLへ転送
- `GET /youtube/info?url=<YouTube URL>` — 動画タイトルまたはプレイリスト一覧を取得し、URLで指定された再生位置を先頭にしてJSONで返却
- `GET /redgifs/resolve?url=<RedGifs URL>` — 公開RedGifs URLを検証してHD優先のMP4 URLをJSONで返却
- YouTubeのHTTPS動画URLだけを許可
- RedGifsは公式サイトのHTTPS視聴・埋め込みURLだけを許可し、公式APIへ問い合わせ
- シェルを使わずyt-dlpを実行
- VRChatと相性のよい映像・音声一体型MP4を優先
- RenderではProof-of-Originトークン付きMWEB方式を優先
- 直接取得に失敗した場合はエラーを返し、Ksyncへ自動転送しない
- サイト上のKsync予備リンクは手動バックアップとして残す
- プレイリストは最大200件（`PLAYLIST_MAX_ITEMS` で変更可能）
- 簡易キャッシュ、同時実行数制限、アクセス回数制限付き
- 通常のアクセスログは無効

## 設置

1. Renderで `Piloton1231/piloton-site` のBlueprintを作成します。
2. 無料プランのWebサービスとしてデプロイが完了するまで待ちます。
3. サービスのCustom Domainsへ `video.piloton.cc` を追加します。
4. DNSのCNAMEをRenderの表示どおりに変更します。
5. `https://video.piloton.cc/health` が `{"status":"ok"}` を返すことを確認します。

## 更新

YouTube側の変更で抽出が動かなくなることがあります。`requirements.txt` のyt-dlpを公式の新しい安定版へ更新し、再ビルドしてください。バージョンを無確認で自動更新する構成にはしないでください。

## 制限と注意

- 非公開、メンバー限定、有料、年齢制限コンテンツには対応しません。
- GoogleアカウントのCookieや認証情報を入れないでください。
- 無料サービスの共有IPはYouTubeに拒否される場合があり、証明トークンを使っても成功は保証されません。
- VercelのPython実行環境だけでは同梱のトークン生成器を常駐できないため、直接転送にはDocker版を使います。
- VRChatで入口ドメインが信頼済みでない場合、「Allow Untrusted URLs」が必要です。
- 公開運用前に、利用規約・著作権、サーバー事業者の規約を確認してください。
- 大規模公開する場合は、アプリ内の簡易制限に加えてファイアウォールやCDN側のレート制限も設定してください。
