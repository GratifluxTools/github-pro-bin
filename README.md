# github-pro-bin

## Actions workflows

### HTTP Proxy

既存の`.github/workflows/proxy.yml`。`proxy.py`を起動し、Pinggy Free TCP tunnelを作ります。

### HTTP Proxy with DNS

`.github/workflows/http-proxy-with-dns.yml`を手動実行すると、同じ`proxy.py`とPinggy Freeを使い、Pinggyが出力するTCP endpointのホスト名をCloudflareの`proxy.rokko-st.com`に一時反映します。DNSレコードの更新方式は、前の`rokko-proxy-actions`にあったCloudflare API upsertを参考にしています。

Pinggy Freeは1セッション最大約60分のため、workflowはPinggy接続終了後に停止します。正常終了またはActionsのCancel時、DNSレコードを元の状態へ戻し、Pinggy SSHトンネルと`proxy.py`を停止します。job timeoutは後処理用の70分です。

#### 必要なGitHub Actions Secrets

Repository **Settings → Secrets and variables → Actions** に設定してください。

- `PROXY_USER` — プロキシユーザー名
- `PROXY_PASS` — プロキシパスワード
- `CF_API_TOKEN` — `rokko-st.com` Zoneの **Zone Read、DNS Read、DNS Write** 権限を持つCloudflare API Token
- `CF_ZONE_ID` — `rokko-st.com` のZone ID

workflowはCloudflare APIでゾーンと`proxy.rokko-st.com`のレコードを照合します。対象名のA/CNAMEが1件ならDNS-onlyのCNAMEへ切り替えます。単一Aレコードの場合は一時削除してCNAMEを作り、終了時に元レコードを復元します。元がCNAMEなら内容とproxy設定を終了時に戻します。競合レコードや複数レコードがあればDNSを変更せず失敗します。

#### クライアントの接続先

ActionsログとSummaryに、例のように表示します。

```text
proxy.rokko-st.com:41639
```

DNSはホスト名をPinggyの`*.run.pinggy-free.link`へ向けますが、DNSではTCPポートを転送できません。Pinggy Freeが毎回割り当てるポートをプロキシ設定に指定してください。**`:8080`には固定できず、端末側のプロキシポートをActionsに表示される値へ設定する必要があります。**

Cloudflare DNSのキャッシュ済み応答は切替直後もしばらく残る場合があります。workflowはPinggy使用中のDNSをDNS-onlyにしてTTL 60秒を指定します。現在の公開応答TTLは300秒なので、初回切替では一部DNSキャッシュが最大約5分間、古い応答を返す可能性があります。
