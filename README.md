# github-pro-bin

## Actions workflows

### HTTP Proxy

既存の`.github/workflows/proxy.yml`。`proxy.py`とPinggy Free TCP tunnelを起動する従来workflowです。

### HTTP Proxy with DNS

`.github/workflows/http-proxy-with-dns.yml`。従来の単一ホスト`proxy.rokko-st.com`を一時的にPinggyへ向けます。

### HTTP Proxy Pool

`.github/workflows/proxy-pool.yml`はallocatorです。手動実行するとGitHub Contents APIのSHA compare-and-swapで稼働slotを確認し、`proxy1.rokko-st.com`から未使用の最小番号を予約して、その番号専用のworkflowをdispatchします。3枠が全て稼働中なら新規runは安全に失敗します。

実働するslotは3つの独立したActions workflowです: `HTTP Proxy Slot 1`、`HTTP Proxy Slot 2`、`HTTP Proxy Slot 3`。必要ならActions画面から個別に手動起動もできます。その場合は固定番号のslotを使います。通常は`HTTP Proxy Pool`を起動すると最小の空き番号を選びます。

稼働確認後、該当サブドメインをPinggy Freeの実際の割当ホストへDNS-only CNAMEで向け、ポート・速度・接続数・開始時刻・終了予測を`proxy-pool.json`へ掲載します。各slot workerは約10分ごとに速度/接続数と更新時刻を更新します。速度はCloudflare download testまでの参考Mbps、接続数はproxy processで観測する同時client session数です。速度テストが失敗した場合は`null`となり、サイトは「計測中」と表示します。

各Pinggy Free接続は最大約60分です。55分経過時（またはSSH tunnelが先に切れた時）に同じ番号の独立した`HTTP Proxy Slot N` workflowを後継として起動します。後継が接続・DNS切替し、一覧へonline状態を掲載するまで旧runは待機し、掲載後にDNS TTL分だけ猶予を置いて旧runのSSH tunnelと`proxy.py`を停止します。各runが終了後、後継workflowへ引き継いで連続稼働します。

GitHub Actionsの**Cancel workflow**は各slot workflow内のPython supervisorが子プロセスグループを停止し、さらに`if: always()` cleanup stepがslot状態、未完了後継run、DNSを確認して後始末します。allocator workflowをCancelした場合も、まだ公開前のslot予約とdispatch済みの子runをcleanupします。

#### 必要なGitHub Actions Secrets

Repository **Settings → Secrets and variables → Actions** に設定してください。

- `PROXY_USER` — プロキシユーザー名
- `PROXY_PASS` — プロキシパスワード
- `CF_API_TOKEN` — `rokko-st.com` Zoneへの **Zone Read、DNS Read、DNS Write** 権限
- `CF_ZONE_ID` — `rokko-st.com` のZone ID
- `PROXY_CONTROL_TOKEN` — fine-grained PAT（対象repositoryの **Actions: Read and write**）。handoff先workflowをdispatchするために使います。

一覧のstateファイルは公開repositoryの`proxy-pool.json`に保存されます。公開内容にはプロキシのホスト名・ポート・測定値・Actions run ID・DNS復元に必要なDNSレコード情報が含まれる場合がありますが、プロキシのユーザー名/パスワードやAPI tokenは含めません。学校サイトはこのJSONを取得します。ウェブページ自体は既存のログインガード配下です。

#### 接続先

一覧に`proxy1.rokko-st.com`等のサーバー名とPinggyの割当ポートが表示されます。**ポートはPinggy Freeの割当値であり8080固定ではありません。** クライアントのプロキシ接続先には表示中のポートを指定してください。

#### 既知の制約

- Cloudflare DNS-only CNAMEが伝播するまで、古いDNSキャッシュが最大TTL程度残る場合があります。
- GitHub tokenのActions/Contents権限またはCloudflare DNS権限が不足すると、割当・引継ぎ・cleanupの該当処理が失敗します。
- Pinggy Freeの最大同時TCPセッション数は公式の公開資料で確認できませんでした。3つの別々のActions runnerからの同時接続がPinggy側で許可されることは保証できず、各slotの実起動状況はActionsログと一覧で確認してください。
- Pinggy FreeのTCP転送は暗号化されないため、HTTP proxy Basic認証情報は転送中に保護されません。資格情報の再利用や機密データの送信は避けてください。

Pinggy公式資料: [Free tunnel timeout](https://pinggy.io/help/)（60分でURLが変わる）、[TCP tunnels](https://pinggy.io/docs/tcp_tunnels/)（接続ごとにTCP portを割当）、[Usage](https://pinggy.io/docs/usages/)（Free TCPのSSH構文とtoken挙動）。
