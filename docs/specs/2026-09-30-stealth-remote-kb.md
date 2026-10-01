# ステルス Remote KB（`kb NAME --remote codex|claude` の透過注入）設計

日付: 2026-09-30（2026-10-01 改訂: Codex は stealth のみ、kb は外部のアカウント中継サービスに依存しない）

## 目的

`--remote` の KB 注入を、クライアント（Codex CLI / Claude Code）の設定・provider・base URL を一切変えずに行う。
現行（2026-09-29 実装）の loopback プロキシ方式は、Codex を custom provider、Claude を別 `ANTHROPIC_BASE_URL` で起動するため、クライアントが「公式ホストではない」と判断して挙動を変える。

- Claude Code: Anthropic 以外の base URL では MCP tool search を止め、全ツール定義を毎要求に前置きする（`ENABLE_TOOL_SEARCH=true` で回避したが、他にも first-party 判定に依存する機能がありうる）。
- Codex: custom provider では ChatGPT ログイン前提の機能（検索、プラグイン、accounts/check に依存する経路など）が使えない。WebSocket も無効にしている。

mitmdump による chatgpt.com / api.anthropic.com の透過横取りを、kb がセッション単位で行う。

## 観測済みの事実（2026-09-30 スパイク）

- Codex CLI 0.156 は `HTTPS_PROXY=http://127.0.0.1:<port>` を尊重し、mitmdump（regular proxy mode）経由で `chatgpt.com` に到達する。CA は macOS keychain に登録した mitmproxy CA で信頼される。
- Codex の推論は `GET /backend-api/codex/responses` の WebSocket upgrade で行われる（HTTP POST ではない）。モデル一覧・プラグイン等は HTTP。
- Claude Code は `HTTPS_PROXY` + `NODE_EXTRA_CA_CERTS=<CA pem>` で `api.anthropic.com` のまま通る（keychain 不要）。
- mitmproxy 12.2.3（`uv tool install mitmproxy`）で動作確認した。mitmproxy は `request` / `websocket_message` フックで HTTP ボディと WS フレームを書き換えられる。

## 構成

```
kb NAME --remote codex|claude [args]
  └─ kb: KB 平文/items を用意、binding を保存（既存）
  └─ kb: mitmdump をランダムポートで起動（-s kb_stealth_addon.py、--allow-hosts chatgpt.com|api.anthropic.com、confdir=CA dir）
  └─ kb: 子プロセス起動。env に HTTPS_PROXY=http://127.0.0.1:<port>（Claude は NODE_EXTRA_CA_CERTS も）
        設定ファイル・provider・base URL・CODEX_HOME 選択（codex ラッパー）は無変更
  └─ addon: 対象要求だけ書き換えて上流へ。それ以外は素通し
  └─ 子プロセス終了 → mitmdump 終了、終了コードは子のもの
```

## 注入規則（既存 injector を流用）

- Codex HTTP `POST chatgpt.com/backend-api/codex/responses`: `input` の先頭 system/developer 直後に items を挿入。`compaction_trigger` / `request_kind=compaction` は素通し。`previous_response_id` 付きは拒否せず、**WS と同じ会話追跡**で扱う（下記）。
- Codex WS `GET chatgpt.com/backend-api/codex/responses`（upgrade）: client→server のテキストフレームで `type == "response.create"` のものに、HTTP と同じ規則で `input` へ挿入する。`previous_response_id` を使う継続要求では、接続内で最初の完全な input に注入し、以降の差分要求は素通しする（`kb_remote_proxy.CodexConversation`）。server→client フレームは触らない。
- Claude `POST api.anthropic.com/v1/messages`: `system` は変更しない。KB は Claude Code 自身の `/compact` 要約と同じ形の user テキストブロック（`This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\n` + KB 本文）にし、**最初の `role: user` メッセージ**に挿入する。位置は先頭の `<system-reminder>` テキストブロック群の直後で、セッション自身の要約ブロック（同じ定型文で始まる）があればその直前。文字列 `content` は `[{"type":"text",...}]` に正規化する。user メッセージがなければ素通し（`proxy.log` に1行）。`messages` 内の `role: system` には触れない。
  - compaction 要求（最後の user メッセージが `<system-reminder>` を除いて `Your task is to create a detailed summary of the conversation so far` で始まる）は Codex の `compaction_trigger` と同様に素通しし、セッションごとに初回だけ `stealth: compaction request, KB not injected` を記録する。
  - 漏れ検知: kb が挿入していない要約ブロックに `## Decrypted KB material:` 等の KB 見出しがあれば、セッションごとに1回 `warning: KB text found inside Claude's compaction summary (anchor may have drifted)` を記録する（ブロックは変更しない）。
  - Claude の loopback provider プロキシと stealth addon は同じ関数（`kb_remote_proxy.claude_edit` → `is_claude_compaction_request` / `inject_claude_user_block`）を使う。
- 上記以外のパス・ホストは byte-for-byte 素通し。`--allow-hosts` で対象 2 ホスト以外は TLS 終端もしない。
- 圧縮ボディ（gzip/zstd）は mitmproxy が透過的に扱う（`flow.request.text` / `set_text`）。

## binding / resume（既存）

- thread/session id の観測は addon 内で行い、kb 本体へは stderr ではなく `~/.cache/kb/bindings/proxy.log` と binding ファイルで伝える（addon は kb 本体とは別プロセスなので、共有はファイル経由）。Codex の id は `client_metadata`/ヘッダ（既存規則）、Claude は `X-Claude-Code-Session-Id`/`metadata.user_id`。
- `kb --remote codex resume [ID]`、`kb --remote claude --resume ID|--continue` は現行どおり。ステルス方式でも同じ binding を使う。
- 旧構文 `kb codex NAME --remote` も stealth で動く。mitmdump を 1 つ起動し、その `HTTPS_PROXY` の下で app-server がスレッドを作成・binding を保存（`--prompt` の最初の依頼もここで実行）してから、同じプロキシの下で `codex resume` を起動する。`--session-only` / `--app` は再開方法（`kb --remote codex resume ID`）を表示して終わる。

## モード選択

- 既定・唯一の自動選択は **stealth**。mitmdump の探索順は `KB_MITMDUMP` 環境変数 → PATH の `mitmdump` → `~/.local/bin/mitmdump`。
- 見つからない場合はフォールバックせず、導入手順（`uv tool install mitmproxy && kb ca-setup`）を出して終了する。
- **Codex は stealth のみ**。`KB_REMOTE_MODE=provider` を指定するとエラー（Codex の custom provider 方式は削除した）。
- Claude Code だけは `KB_REMOTE_MODE=provider` で従来の loopback プロキシ（`ANTHROPIC_BASE_URL`）を明示的に選べる。
- kb は外部のアカウント中継サービスの有無で分岐しない。Codex の設定 override（`KB_CODEX_CONFIG_OVERRIDES`）も引き継がない。
- kb が使う外部エンドポイントは、KB 作成の compaction と `kb decrypt` 用の OpenAI Responses 互換 RR エンドポイント（`KB_RR_BASE_URL` / `KB_RR_CONFIG`、README 参照）だけで、`--remote` は使わない。

## CA

- CA ディレクトリは `KB_CA_DIR`（既定 `~/.cache/kb/ca`）。無ければ mitmdump 初回起動で生成される。
- `kb ca-setup`（新サブコマンド）: CA の場所と、信頼登録の手順を表示する。macOS は
  `security add-trusted-cert -d -r trustRoot -k ~/Library/Keychains/login.keychain-db <ca dir>/mitmproxy-ca-cert.pem`、Linux は `update-ca-certificates` / `trust anchor` の案内。Claude は `NODE_EXTRA_CA_CERTS` で足りるが Codex は OS 信頼が必要。
- Codex が CA を信頼していない場合、起動直後の要求が TLS エラーになる。addon/kb はそれを検知して「`kb ca-setup` を実行」と 1 行出す（`tls_failed_client` フック）。

## エラー処理

- mitmdump 起動失敗（ポート衝突・CA 生成失敗）: provider 方式にフォールバックせず、理由と `kb ca-setup` の案内を出して終了する。
- 子プロセスが先に終了したら mitmdump を必ず止める（`finally`）。
- addon 内の例外は上流へ素通し（注入せず）し、proxy.log に 1 行。TUI には出さない。

## テスト

- 純関数: WS `response.create` の注入と `previous_response_id` 追跡、HTTP 注入、Claude の user ブロック注入・compaction 素通し・漏れ検知。
- addon: mitmproxy の `tflow`/`taddons` テストユーティリティで、`request` / `websocket_message` フックが対象要求だけを書き換え、他ホスト・他パスを触らないこと。
- ランチャ: mitmdump のダミー実行ファイルを PATH に置き、子プロセスの env（`HTTPS_PROXY`、Claude の `NODE_EXTRA_CA_CERTS`、`ANTHROPIC_BASE_URL` 不在、provider override 不在）と終了コード伝播、Codex の provider 指定がエラーになること、フォールバックしないことを確認。
- E2E（手動、Herdr の TUI）: `kb octane --remote codex` で `/status` が provider=openai のまま、WS で KB 質問に回答、`resume` で再注入。`kb octane --remote claude` で `/context` の MCP tools が on-demand のまま、KB 質問に回答、`--resume` で再注入。

## 対象外

- 上流（chatgpt.com / api.anthropic.com やその手前の中継）側の変更。
- Codex App / ChatGPT App の透過（プロセス単位の env なので対象外、以前の仕様どおり）。
- Windows。
