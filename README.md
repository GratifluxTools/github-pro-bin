# github-pro-bin

## Pinggy Free rotating proxy workflow

`.github/workflows/pinggy-rotation.yml` は手動起動する、無料Pinggy TCPトンネルのローテーションworkflowです。起動時の `auto_switch` を `true` にすると、55分ごとに次のPinggyトンネルを先に確立し、`proxy.rokko-st.com` のDNS CNAMEを新しいPinggyホストへ更新してから旧トンネルを停止します。標準の最大実行時間は5時間55分です。`false` の場合は切替を行わず、最初のPinggy接続が切れるか最大実行時間に達した時点で終了します。

### 必要なGitHub Actions Secrets

Repository **Settings → Secrets and variables → Actions** に以下を設定してください。

- `PROXY_USER` — プロキシユーザー名
- `PROXY_PASS` — プロキシパスワード
- `CF_API_TOKEN` — Cloudflare API Token。`rokko-st.com` Zoneに対する **DNS Read** と **DNS Write** のみを付与
- `CF_ZONE_ID` — `rokko-st.com` のZone ID

`CF_API_TOKEN` はログへ出しません。Cloudflare APIで `proxy.rokko-st.com` の既存DNSレコードを読み、ちょうど1件のCNAMEであることを確認してから更新します。A/AAAAなど競合レコードがある場合は安全のため失敗し、削除・置換はしません。既存CNAMEがなければ一時CNAMEを作成します。

Pinggy接続中はCNAMEを **DNS-only（Cloudflareプロキシ無効）**、TTL 60秒で設定します。各ローテーションでは旧DNSキャッシュの猶予後に旧Pinggyトンネルを停止します。現時点の公開DNSはCloudflare経由でTTL 300秒のA/AAAA応答です。初回切替直後は既にキャッシュされた古いCloudflare応答が最大約5分残る可能性があります。Cloudflare APIで対象名に単独のAレコードが見つかった場合は、そのAレコードを一時削除してCNAMEを作り、通常終了・workflow cancel時に元のAレコード設定を再作成します。単独のCNAMEなら内容を一時変更して終了時に元へ戻します。事前レコードがなければ一時CNAMEを作成して終了時に削除します。複数レコードやAAAAなど競合があれば変更せず失敗します。

### クライアントの接続先

DNSはホスト名をPinggyへ向けますが、**DNSにはTCPポート番号を格納できません**。Pinggy FreeはトンネルごとにTCPポートを割り当てるため、接続先はホスト名だけの `proxy.rokko-st.com:8080` には固定できません。Actionsの実行ログとSummaryに表示される `proxy.rokko-st.com:<割当ポート>` を、切替の都度クライアントのプロキシポートへ設定してください。端末が自動的にポートを追従する仕組みではありません。

### 停止・失敗時

- GitHub Actionsの **Cancel workflow** はPythonオーケストレーターへ終了シグナルを送り、全Pinggy SSHプロセスグループと `proxy.py` を停止し、可能な範囲でDNSを元へ戻します。
- 実行時間の上限到達時は同じcleanupを行います。Workflow jobの6時間timeoutはcleanup用の最後の安全弁です。
- Pinggy Freeが同一アカウント・送信元からの同時接続を拒否する、DNS API権限不足、Cloudflare側に競合レコードがある等の場合、handoffは失敗し、Actionsログに理由を表示します。
- 受信先のDNSはworkflow実行中だけPinggy向けです。Actionsが停止している間は元のDNSレコードへ戻ります。

関連ソース: `proxy.py`、`.github/scripts/pinggy_rotation.py`、`.github/workflows/pinggy-rotation.yml`。
