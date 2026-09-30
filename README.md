# kb-repomap

元リポジトリはGit URLのほか、SSHマシン名＋絶対パスでも登録できます。
SSH別名・鍵は通常のGit/SSH設定を使います。対象のコミット済みデータを取得します。

```bash
kb register my-code --source https://github.com/owner/repository.git --branch main
kb register my-code --host user@server --path /home/user/repository --branch main
# 対話入力にも server:/absolute/path 形式を指定可能
```

Gitリポジトリを **Aiderの構造マップ → v2圧縮blob → 持ち運べるKB** にするツールです。
保存するのはKB項目のJSON配列だけです。利用時に、そのPCのCodex設定で新しいセッションを作ります。

`kb-multiblob` から独立し、LLMによる概要調査をAiderから切り出したrepo-mapエンジンへ
置き換えました。圧縮は `responses + compaction_trigger` を使うCodexの新方式です。

## インストール

```bash
git clone https://github.com/yukimaru77/kb-repomap.git
cd kb-repomap
uv sync --locked
npm ci --ignore-scripts
```

Python 3.12とNode.js/npmの依存関係をプロジェクト専用環境へ用意します。Aider本体・LiteLLM・Codex CLIは
map/blob生成には不要です。初回のパッケージ・tiktoken辞書取得にはネットワークを使いますが、
map生成自体にLLM呼び出しやAPIキーは不要です。

**初めてKBを作るPCでは、先に `~/.config/kb/config.json` に号池の接続先とキーファイルの
場所を設定してください。** [初回の設定手順](#作成再作成時の接続先とオプション)は下記にあります。
インストーラーはAPIの接続先・キーを自動設定しません。

## 基本の4コマンド

```bash
bash install.sh
kb register xx   # 対話で登録
kb create xx     # 基準ブランチからKBを作成し、保存先へcommit/push
kb list          # 登録済みKBと作成状態を一覧表示
kb xx codex      # KB入りの新規Codexセッションを開く
```

`kb register` は次を質問します。KB名を引数で渡した場合、名前の質問は省略します。

1. KB名
2. 元リポジトリのURL
3. 基準ブランチ（既定: `main`）
4. 保存先GitリポジトリのURL（登録済みの保存先があれば表示）

**自分専用の保存先には、プライベートリポジトリを作成して使うことをおすすめします。**
初回commitと既定ブランチを用意してから、そのURLを指定してください
（GitHubなら作成時にREADMEを追加すると用意できます）。

保存先には既存のGitリポジトリを指定します。新しいURLならローカルの保存先一覧にも登録し、
同じURLを登録済みならその設定を使います。登録時は `<KB名>/info.json` を保存し、
`source_commit` は `null`（未作成）になります。登録だけでは推論APIを呼びません。
既存の同名KBへの再登録は上書きせずエラーになります。

`kb create xx` は登録されたURL・ブランチから、その時点のHEADを固定して作成します。
成功すると `info.json` に作成commitを記録し、`latest.json` と一緒にcommit/pushします。
JSON配列には暗号化compaction項目と短いKB憲章（user指示）だけを保存します。
`session_meta`、`base_instructions`、作成者のセッションID・会話履歴は含めません。
作成時にCodexセッションを鋳造する工程はなく、既存のローカルセッションも不要です。
作成済みKBに実行すると作り直します。登録した保存先を指定する場合は
`kb create xx --store <保存先名>` を使います。

作り直しは既定で増分です。保存済みKB（同名ファイル、なければ `latest.json`）の
`pack_manifest` を再利用元にし、基準ブランチの差分で影響を受けたパックだけ号池で圧縮し、
残りのblobはそのまま流用します。実行時に `blob再利用: N/M / 号池で圧縮: K` を表示します。
前回blobを使わず全体を作り直す場合は `--clean` を付けます。
`kb create`、`kb codex --rebuild always`、`kb NAME --rebuild always codex`、
再作成の質問に `y` と答えた場合のいずれも同じ増分処理です。

```bash
kb create octane            # 増分（既定）
kb create octane --clean    # 全体を作り直す
kb octane --remote --rebuild always --clean codex
```

作成には下記の `build_args` または `KB_POOL_*` による圧縮API接続先の設定が必要です。
`kb list` では未作成KBを「未作成」と表示します。作成後は同じ名前で `kb codex xx` を使えます。

保存済みblobの平文は `kb decrypt xx` で復元します。blob が N 個なら、各波は high と max を同時に N 本ずつ、合計 2N 並列です。順番は次の4波で、最大8回です。

1. `gpt-6-luna` high と max
2. `gpt-6-luna` high と max の2回目
3. `gpt-5.6-luna` high と max
4. `gpt-5.6-luna` high と max の2回目

blobのトークン数から±2%以内のテキストが出たblobは、その波で打ち切ります。範囲内が複数なら、いちばん近いトークン数を採用します。最後まで範囲内がなければ、最も近いトークン数を採用します。出力したテキスト、採用した `raw.txt`、選定理由を `<KB名>/decrypt/<ファイル名>/` にcommit/pushします。完了済みの呼び出しは再実行しません。

### KBの保存名を指定する

`--file` で `latest.json` 以外の名前を指定できます。省略時は `latest.json`。
拡張子 `.json` は省略でき、`--file v1.00` と `--file v1.00.json` は同じファイルを指します。
`create`・`publish`・`codex`（`--remote` を含む）で共通です。

```bash
kb create octane --file v1.00
kb codex octane --file v1.00
kb list
```

同じ名前で再作成すると、そのKBと作成情報を上書きしてcommit/pushします。
別名のファイルや `latest.json` は変更しません。`--file` にはパスを含まないファイル名を
指定します。拡張子 `.json` / `.jsonl` がなければ `.json` を補います。空白を含む名前は引用符で囲みます。

`kb list` はファイルごとに、KB名・保存先・作成commit・基準ブランチ・ファイル名を表示します。
別名だけを作成した場合、`latest.json` の行は「未作成」のままです。
`kb codex --file ...` の起動時も、そのファイルの作成commitから差分を確認します。

## Claude Codeで復号済みKBを開く

復号済みのKBをClaude Codeへ渡す場合は、保存先の `dev.txt` と
`decrypt/<ファイル名>/` 配下の選択済み `raw.txt` を初期システムコンテキストにして、
新しいローカルClaudeセッションを起動します。号池は使用しません。

```bash
kb octane claude --model sonnet
kb octane --file v1.00 claude --permission-mode bypassPermissions
```

`decrypt` の成果物がない場合は、先に `kb decrypt octane` を実行してください。
再作成を選んだ場合は同じファイル名を更新します。

### `--remote` でリクエストごとにKBを挿入する

`--remote` を付けると、KBを起動引数に載せず、kbが起動中だけローカルのプロキシ
（`127.0.0.1` のランダムポート）を立てます。`POST /v1/messages` の各リクエストで、
**最初の user メッセージ**へKBのテキストブロックを1つ挿入してから Anthropic API へ転送します。
`system` は変更しません。
認証ヘッダーやSSE応答はそのまま中継し、`count_tokens` などの他のパスは変更しません。
既定は **stealth 方式**で、Claude Code の設定・base URL は変えず `HTTPS_PROXY` と
`NODE_EXTRA_CA_CERTS` だけを渡します（[`--remote` の方式](#--remote-の方式stealth--provider)）。
以下の `ANTHROPIC_BASE_URL`・`ENABLE_TOOL_SEARCH` の項目は provider 方式のときだけです。

```bash
kb octane --remote claude --model sonnet
kb octane --file v1.00 --remote claude -p "要点を教えて"
```

- KBブロックは Claude Code 自身の `/compact` 要約と同じ形です。本文は
  `This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.`
  で始まり、続けて `## Decrypted KB material: ...` の各見出しが並びます。
  挿入位置は、先頭に並ぶ `<system-reminder>` ブロックの直後です。セッション自身の要約ブロックがあれば、その直前に置きます。
  `content` が文字列の場合はテキストブロック1つの配列に直します。user メッセージがない要求は変更せず、`proxy.log` に1行記録します。
- **compaction 要求にはKBを入れません**。最後の user メッセージが（`<system-reminder>` を除いて）
  `Your task is to create a detailed summary of the conversation so far` で始まる要求は、Claude Code の要約要求としてそのまま転送します。
  Codex の `compaction_trigger` と同じ扱いで、要約にKBの本文が混ざるのを防ぎます。
  セッションごとに最初の1回だけ `proxy.log` に `stealth: compaction request, KB not injected` を記録します。
- **漏れ検知**: kbが挿入したものではない要約ブロック（上の定型文で始まるもの）に `## Decrypted KB material:` などKBの見出しが
  含まれていれば、`proxy.log` に `warning: KB text found inside Claude's compaction summary (anchor may have drifted)` を
  セッションごとに1回記録します。ブロック自体は変更しません。Claude Code の要約指示の文言が変わった可能性を示します。
- 事前に `kb decrypt octane`（`--file` を使う場合はそのファイル）を実行しておく必要があります。
- KBの平文が `ps` やコマンドライン長の制限に乗らず、大きなKBもファイル読み込みなしで渡せます。
- provider 方式: 既に `ANTHROPIC_BASE_URL` を設定している場合は、このセッションだけkbのプロキシで上書きします。
- Claude Codeの終了とともにプロキシも停止し、終了コードはClaude Codeのものを返します。
- 号池は使用しません。provider 方式の転送先は `KB_CLAUDE_UPSTREAM`（既定 `https://api.anthropic.com`）で変更できます。
- provider 方式では `ENABLE_TOOL_SEARCH=true` を既定で設定します。Claude Codeは Anthropic 以外の base URL では
  MCPツールの遅延読み込みを止めて全ツール定義を毎要求に前置きする（環境によっては16万トークン超）ため、
  kbのプロキシは tool_reference をそのまま転送し、通常どおりの必要時読み込みを保ちます。
  自分で値を設定していればそれを優先します。
- Claude Code のコンテキスト表示はモデルIDで窓の大きさを決めます。大きなKBでは
  `--model 'claude-fable-5-1[1m]'` のように `[1m]` 付きIDを渡すと 1M 窓として計測されます。
- セッションIDはkbが作成し、起動時に binding（後述）を保存します。再開は
  `kb --remote claude --resume ID` で行います（[`--remote` セッションの再開](#--remote-セッションの再開)）。

旧版の `latest.jsonl` や名前付きJSONLも読み込めます。指定した `.json` がなく、同名の
`.jsonl` がある場合は自動で旧ファイルを読みます。どちらもなければエラーになります。
旧JSONLからもKB項目だけを抽出し、保存元のシステム指示やセッション情報は引き継ぎません。
既存の保存先を一括変換しなくても利用できます。新しく保存する内容はKB項目のJSON配列です。
同じ保存名を新形式で保存すると、旧 `.jsonl` は最新のGitツリーから取り除きます。
過去のセッション形式はGit履歴に残ります。

## Gitに保存したKBを `kb codex` で開く

### paper-kb の論文KBを同じ保存先で使う

完成した `paper-kb` の出力ディレクトリを指定すると、引用blob＋本論文blobの列を
同じ保存先へcommit/pushできます。再圧縮は行いません。

```bash
kb publish-paper my-paper --run /path/to/paper-kb/runs/my-paper --store research
kb list --store research
kb codex my-paper --store research
kb codex my-paper --store research --remote
```

公式資料付きの最終run（`report.json`形式）も同じコマンドで保存できます。
本論文の先頭blobとcheckpoint、公式資料blob、両方の入力Gitのcommitと資料ハッシュを検証し、
公開先にはblob列と必要な出典・件数だけを保存します。元runのmanifestやアカウント情報はコピーしません。
入力Gitがローカルパスの場合は、元commitを履歴に保持した公開用Git URLを指定してください。

```sh
kb publish-paper my-paper --run /path/to/final-run --store research \
  --source-repository-url https://github.com/OWNER/paper-sources.git \
  --main-source-repository-url https://github.com/OWNER/paper-sources.git
```

前者は公式資料、後者は本論文の入力Gitです。指定先が元commitを取得できることを確認し、
commitを固定して登録します。別の履歴の上流リポジトリを代わりに指定することはできません。
元runや`result.json`を書き換える必要はありません。

`--file v1` で名前付き保存も可能です。保存するのは `latest.json`（blob列）と
`info.json`（論文種別・元Git URL・コミット・ブランチ・論文サブディレクトリ・入力SHA256・引用資料数・引用blob数・本論文blob数・生成設定）です。
接続先キー・アカウント一覧・作成者のセッション設定は保存しません。
論文KBも元Gitリポジトリを必須とし、保存時に指定コミットを取得できることを確認します。
起動時は基準ブランチの更新を確認し、更新があれば再作成方法を表示します。
本文・図を読み直す必要があるため、コード用の差分追加で論文KBを自動更新しません。
再作成は `paper-kb` で行い、再度publishします。`kb create` や
`kb codex --rebuild always` は論文KBでは使用しません。


### 推論時だけKBを挿入する（`--remote`）

新しい起動構文は `kb <KB名> [KBオプション] codex [Codexの引数...]` です。
`codex` より後はCodex自身が解釈します。KB用の `--remote`・`--store`・`--file`・
`--rebuild` は `codex` より前に置きます。

通常起動・`--remote` ともに、次の英語の案内を `role: developer` で渡します。
保存済みの暗号化blobは変更せず、旧「KB憲章」は起動時にこの案内へ置き換えます。
新構文のRemote KBでは案内もプロキシが挿入し、旧構文ではセッション側へ一度だけ挿入します。
KBの準備だけでは user メッセージや推論を自動で開始しません。
新構文の依頼文・標準入力はCodex自身へ渡し、旧構文は明示した `--prompt` だけを実行します。

> The supplied compacted context is prior knowledge provided by the user. Use it as a foundation for subsequent understanding and work. When precise details are needed, use that knowledge to narrow down relevant sources and search them efficiently.

保存先Gitリポジトリの各KBディレクトリに `dev.txt` を置くと、その内容も追加の
`role: developer` メッセージとして渡します。例えば `molmoact2/latest.json` と
同じ場所の `molmoact2/dev.txt` に、`./paper/paper.md (Full paper text in Markdown)`
のような資料のパスと一言の説明を英語で記述できます。
`dev.txt` はUTF-8のプレーンテキストで、同じKBフォルダ内の全バージョンに共通です。
KBと同じ保存先commitから取得し、元資料のリポジトリや現在の作業ディレクトリにある
`dev.txt` は自動では読みません。ファイルがない場合や空の場合は従来どおりです。

新旧の起動構文・TUI・`exec`・`--app`・`--remote`・`pool-rr` で共通です。
ローカル方式ではセッションへ挿入し、Remote方式ではkbのプロキシが各推論へ挿入します。
暗号化blobや `latest.json` 自体は変更しません。編集を保存先へpushすると次回の
新規起動から反映され、すでに起動したセッションの指示は変わりません。

```bash
# TUI
kb octane --remote codex -m gpt-6-astra
# 非対話実行、JSONLイベント、最終回答の保存
kb octane --remote codex exec --json -o answer.txt "設計を説明して"
# ラウンドロビンと併用
pool-rr kb octane --remote codex exec -m gpt-6-astra "レビューして"
# 標準入力はCodexへそのまま渡す
cat question.txt | pool-rr kb octane --remote codex exec -
# Codexのreviewやヘルプもそのまま
kb octane --remote codex review --uncommitted
kb octane --remote codex exec --help
```

既定の stealth 方式では、Codexの設定・provider・ログインは変えず、`HTTPS_PROXY` だけを渡して
chatgpt.com への推論（WebSocket の `response.create`、HTTPの `POST /backend-api/codex/responses`）に
KBを挿入します（[`--remote` の方式](#--remote-の方式stealth--provider)）。
以下は明示指定時の provider 方式（`pool-rr` 併用時も）の説明です。

provider 方式の `--remote` は、Codexの起動中だけkbがローカルのプロキシ（`127.0.0.1` のランダムポート）を
立て、起動するCodexプロセスだけに、そのプロキシを指す一時的なproviderを `-c` で設定します。
プロキシは `POST .../responses` の `input` について、先頭の `role: system`/`developer` 項目の直後へ
KB項目を挿入し、号池のクライアントキーを `Authorization` に付けて号池へ転送します
（Codex側には仮のキーしか渡しません）。`compaction_trigger` を含む要求・`request_kind: compaction`・
`/responses/compact`・`/models` などは変更せずに転送します。`previous_response_id` 付きの要求は
履歴全体がないためKBを挿入できず、プロキシが400で拒否します（HTTPのSSE接続では送られません）。
号池は単なるアカウントの中継で、号池側のKB登録（`/_pool/kb/bind`）は使いません。
Codexが作った本来のセッションをそのまま使うため、`exec` を `exec resume` に変換せず実行します。
最初の余分な推論もありません。
通常は号池のfill-first経路（`<origin>/backend-api/codex`）、`pool-rr` 付きならRR経路へ転送します。
`pool-rr` のproviderは `base_url` だけをプロキシへ向け、WebSocketは使いません。
モデル一覧は通常のCodexが保存した `~/.codex/models_cache.json`（`CODEX_HOME`対応）を使います。
起動時のKB診断はstderrへ出し、stdout・stdin・終了コードはCodexのままです。
Codexの永続設定・ログイン状態は変更しません。provider自体を別サービスへ変える
`-c model_provider=...` などとは併用しないでください。
Codexの終了とともにプロキシも停止します。最初の推論要求で見えたセッションIDの binding を保存します。

`--remote` を省略したローカルKBでは、KBを注入したセッションを作り、TUIまたは
`exec resume` で起動します。Codexのオプションはインストール済みCLIのヘルプから
判別して渡します。`review` や既存セッションを指定する `resume`・`fork` には
`--remote` を使ってください。`--ephemeral` を指定しても、ローカルKBを注入する
準備用セッションは保存されます。
ローカル方式の準備時には `-c` と `-m` を適用しますが、`-p` や
`--ignore-user-config` などで初期プロンプトの設定まで完全に切り替える場合は
`--remote` を使ってください。

新構文では再作成の既定値は `--rebuild never`。元コードの更新差分はKBとともに渡し、
対話入力を先に消費しません。再作成したい場合は `--rebuild always` を明示します。

従来の `kb codex <KB名> ...` も互換用に利用できます。

```bash
kb codex octane --remote
kb codex octane --remote --file v1.00.json --store work
kb codex octane --remote --session-only --rebuild never
```

`--remote` は、新しいネイティブCodexセッションを作り、kbのプロキシ経由で `codex resume` する。
KB本体をローカルのCodex履歴に入れず、プロキシが推論要求の先頭のsystem/developer項目の後へ挿入する。
会話のcompaction blobがある場合も、その前に置く。コンパクト要求にはKBを含めない。
案内（developer）と更新差分はセッション側へ一度だけ保存し、プロキシはそれ以外のKB項目を挿入する。
サブエージェントは同じCodexプロセスから送られるため、同じKBが挿入される。

挿入するのは保存されたKB項目の配列。複数の独立blobとKB憲章の順序を保つ。
起動時にその内容を固定するため、あとで `latest.json` を更新しても稼働中のセッションは変わらない。
Gitの更新確認・再作成の質問・再作成しない場合の差分追加は、通常の `kb codex` と共通。
`--prompt`、`--file` も併用できる。`--app` と `--session-only` ではkbの終了とともにプロキシも
止まるため、その後のKB付きの続行は `kb --remote codex resume ID` で行う。

接続には既存の `~/.config/kb/config.json` の `build_args` にある `--pool-config` で
号池の共通JSON（例: `codex-account-pool/bridge.json`）を指定できる。
既存の `--origin`、`--key-file`、必要なら `--private-http` と `KB_POOL_*` も使える。
`pool-rr` が渡す `KB_POOL_ORIGIN`、`KB_POOL_KEY_FILE`、`KB_POOL_PRIVATE_HTTP` は保存済みの指定より優先する。
Codexの永続設定・環境は変更しない（子プロセスにだけ仮のキー `KB_NATIVE_POOL_KEY` を渡す）。
**Macの透過ブリッジが同じ号池へ接続していること**が必要。号池側のRemote KB機能は不要。

### `--remote` の方式（stealth / provider）

`kb NAME --remote codex|claude` と `kb --remote codex|claude ...`（再開）は、次のどちらかで動きます。
起動時の stderr の `Remote KB: proxy 127.0.0.1:<port> / ... / stealth`（または `/ provider`）で確認できます。

- **stealth（既定）**: `mitmdump` が見つかれば、kbがセッションごとに mitmdump を
  `127.0.0.1` のランダムポートで起動し、クライアントには `HTTPS_PROXY`（Claude Code は
  `NODE_EXTRA_CA_CERTS` も）だけを渡します。TLSを終端するのは `chatgpt.com` と
  `api.anthropic.com` だけで、それ以外のホストは中身に触れずに中継します。
  書き換えるのは Codex の `/backend-api/codex/responses`（WebSocket・HTTP）と Claude の
  `POST /v1/messages` だけです。クライアントの終了で mitmdump も止まり、終了コードはクライアントのものです。
- **provider（明示指定のみ）**: 従来の loopback プロキシです。Codex は一時的な custom provider、
  Claude は `ANTHROPIC_BASE_URL` でkbのプロキシを指すため、クライアントから見える挙動が変わります。
  そのため**自動では使いません**。mitmdump が見つからない・起動できない場合は、導入手順
  （`uv tool install mitmproxy && kb ca-setup`）と `KB_REMOTE_MODE=provider` の案内を出して終了します。

stealth 方式でクライアント側に**変わらないもの**:

- 設定ファイル・ログイン状態・`CODEX_HOME`（`codex` ラッパーの選択を含む）。
- Codex の provider（`/status` は `provider: openai` のまま）と WebSocket 通信、ChatGPT ログイン前提の機能。
- Claude Code の base URL（`api.anthropic.com`）と MCP tool search（`/context` の MCP tools は必要時読み込みのまま）。
  kbは `ANTHROPIC_BASE_URL`・`ENABLE_TOOL_SEARCH`・`-c model_provider=...` を設定しません。

方式の選択:

- 既定は stealth で、フォールバックはありません。`KB_REMOTE_MODE=provider` を明示したときだけ provider 方式になります。
  mitmdump が見つからない・起動できない場合は案内を出して終了します。
- **`pool-rr` 併用時（`KB_CODEX_CONFIG_OVERRIDES` がある Codex）は常に provider 方式**です。
  RRの入口は号池のURLを指す必要があるためです。
- mitmdump の探索順は `KB_MITMDUMP` → `PATH` の `mitmdump` →
  `~/projects/codex-account-pool/bridge/.venv/bin/mitmdump` です。

CA（`KB_CA_DIR`、既定 `~/.cache/kb/ca`）は mitmdump の初回起動で生成されます。
`kb ca-setup` でCAの場所・mitmdumpの有無・信頼登録の状態と手順を表示します（CAが無ければ生成します）。

```bash
kb ca-setup
# macOS（Codexに必要。Claude Code は NODE_EXTRA_CA_CERTS で足ります）
security add-trusted-cert -d -r trustRoot -k ~/Library/Keychains/login.keychain-db ~/.cache/kb/ca/mitmproxy-ca-cert.pem
```

Linux は `update-ca-certificates`（`/usr/local/share/ca-certificates/` へコピー）か `trust anchor` で登録します。
Codex がCAを信頼していないと推論がTLSエラーになり、kbは終了後に「`kb ca-setup` を実行」と1行表示します。
mitmdump と addon の診断は `~/.cache/kb/bindings/proxy.log`（mitmdump自身の出力は同じ場所の
`mitmdump.log`）にだけ書き、TUIには出しません。KB本体はセッション中だけ権限 0600 の一時ファイルで
mitmdump に渡し、終了時に削除します。

### `--remote` セッションの再開

kbは `--remote` で起動したセッションごとに、`~/.cache/kb/bindings/<セッションID>.json` へ
binding を保存します。

```json
{"client": "codex", "name": "octane", "store": "pepabo", "file": "latest.json", "created": "2026-09-29T14:00:25+00:00"}
```

- Claude: kbがセッションIDを作るため、起動時に保存します。
- Codex: 最初の `/responses` 要求の `client_metadata`（`x-codex-turn-metadata` の `thread_id` など）や
  `Session-Id`/`Thread-Id` ヘッダーからセッションIDを取り、保存します。どの値を使ったかは
  binding の `id_source` と `~/.cache/kb/bindings/proxy.log` に残ります。

再開時はKB名を指定せず、kb経由でクライアントを起動します。

```bash
kb --remote codex resume 01a0ed77-74f8-74c1-9e1e-154c926b22ea
kb --remote codex resume            # TUIの選択画面や --last も可
kb --remote codex exec resume --last "続きをお願いします"
kb --remote claude --resume 6222add9-51b5-486c-85a6-168b4f46e295
kb --remote claude --continue
```

- binding の保存先・KB名・ファイル名から、**再開時点の保存先の内容（最新のKB・dev.txt・更新差分）**を
  読み直して挿入します。起動時のKBではありません。
- IDを指定した場合は起動前に binding を確認します。見つからないIDは「kbの `--remote` セッションでは
  ない」と1行表示し、KBを挿入せずにそのままクライアントを起動します。
- stealth・provider のどちらの方式でも同じ binding を使います。
- IDを指定しない再開（`codex resume` の選択画面・`--last`、`claude --continue`・`--resume` のみ）では、
  プロキシを起動しておき、最初の推論要求に含まれるセッションID（Claudeは `X-Claude-Code-Session-Id`）
  から binding を探します。見つからなければKBを挿入せずに転送します。
- 再開やforkで新しいセッションIDが使われた場合も、同じKBの binding を追加で保存します。
- **kbを経由しない通常の `codex resume` や `claude --resume` では、KBは挿入されません。**
  Remote KBの本体はセッション履歴に保存されていないためです。

### 通常のローカルKB

```bash
bash install.sh
kb store add work https://github.com/your-account/kb-store.git
kb store add personal git@example.com:your-account/another-kb-store.git
kb store list
kb list
kb codex octane
```

保存先は通常のGitリポジトリです。組織・公開範囲・ホストの固定はありません。
各保存先には初回commitと既定ブランチを作っておきます。Gitの既存の認証設定を利用します。
`kb store add <名前> <URL> --branch <ブランチ>` で保存先のブランチも指定できます。
同名で再登録すると接続先を更新し、`kb store remove <名前>` でローカルの登録を取り除きます。

保存先は登録順に検索します。同名KBが複数ある場合は最初のものを使い、
`kb codex octane --store work` で保存先を指定できます。
既定の配置は `info.json` と `latest.json`。別名保存ではKBファイルごとに作成情報を持ちます。

```text
octane/
  info.json
  latest.json
  v1.00.info.json  # --file v1.00.json で保存したKBの作成情報
  v1.00.json
```

`info.json`（別名保存の `v1.00.info.json` も同じ形式）:

```json
{
  "repository_url": "https://github.com/owner/repository.git",
  "source_commit": "KBを作成したcommitの完全なSHA",
  "branch": "main"
}
```

名前はディレクトリ名です。保存するKBにsession IDはありません。
複数blobの順序・型・未知のフィールドはそのまま保存します。
KBと対応する作成情報を同じcommitで更新し、過去版はGit履歴に残ります。

起動時の流れ:

1. 保存先Gitリポジトリをfetchし、同じcommitにある指定KBと対応する作成情報を取得。
   省略時は `info.json` と `latest.json`（旧 `latest.jsonl` も読み込み可能）。
2. 元リポジトリの基準ブランチをfetchし、KB作成時commitと比較。
3. commitが異なる場合に「KBを作り直しますか？ [y/N]」と質問。
   - **Yes**: 既存のrepo-map/v2圧縮処理でその時点のHEADから作り直し、選択された保存先にcommit/push。
   - **No**: 既存KBに加えてcommit一覧とGit diffを使う。保存済みKBは更新しない。
4. ローカルのCodex app-serverの `thread/start` で新しいセッションを作り、
   `inject_items` でKB項目を挿入する。システム指示などは利用者のローカル設定から構築する。
   developer の事前知識ガイダンスと、必要な更新差分を保持する。
5. そのセッションを `codex resume` で開く。`--prompt` がある場合だけ、
   更新差分と指定した依頼文で最初の推論を実行し、応答を待ってから開く。

`--prompt` を省略すると、起動時の推論は発生しません。基準ブランチと同じcommitなら質問は出ません。
`--rebuild always` / `--rebuild never` で回答を指定できます。
標準入力がEOFの場合は再作成せず差分を追加し、その旨を表示します。
取得・再作成・保存に失敗した場合はエラーを表示し、黙って古い内容で起動しません。

作業場所は現在のディレクトリです。`--workspace /path/to/work` で変更できます。
`--session-only` はセッション作成まで、`--app` はCodex Appで開きます。
`--prompt '依頼内容'` で最初の依頼を指定できます。既定はFull Accessで、
`--no-yolo` はCodexの既定権限を使います。Codexの設定・ログインは変更しません。

### 作成済みKBを保存先へ登録する

KB項目のJSON配列を指定します。旧セッションJSONLも入力できます。

```bash
kb publish octane --store work \
  --repository-url https://github.com/owner/repository.git \
  --source-commit <完全なSHA> --branch main \
  --kb /path/to/kb.json
```

`--kb` は読み込むローカルファイル、`--file v1.00.json` は保存先でのファイル名です。
`--jsonl` は互換用の `--kb` 別名として使えます。旧JSONLを渡してもKB項目だけを保存します。
`--file` を省略すると `latest.json` に保存します。同名なら上書きします。

### 作成・再作成時の接続先とオプション

このツールの設定は `~/.config/kb/config.json` に保存します。`stores` は `kb store add` が管理します。
`kb create` と起動時の再作成で使用するビルダーの引数は、同ファイルの `build_args` で指定します。
キー本体は設定や保存先リポジトリに入れず、ローカルのキーファイルを指定してください。

**号池の `bridge.json` があるPCでは、接続情報を複製せず、そのファイルを参照できます。**
既存の `stores` は残したまま、`build_args` の接続設定を次のようにします。

```json
{
  "stores": [],
  "build_args": [
    "--pool-config", "~/codex-account-pool/bridge.json",
    "--workers", "12"
  ]
}
```

共通JSONでは `origin`、`private_http`、`key_file` を読みます。`key_file` がなければ
`state_dir/client.key`、`state_dir` もなければ `state/client.key` を使います。
`key_file` と `state_dir` の相対パスは共通JSONがあるディレクトリを基準にし、`~` も展開します。
たとえば次のJSONは、その隣の `state/client.key` を読みます。

```json
{
  "origin": "https://your-pool.example",
  "private_http": false,
  "state_dir": "state"
}
```

接続時にJSONを読み直すため、同じファイルの `origin` を変更すると後続の接続へ反映されます。
明示したJSONが存在しない場合はエラーになります。環境変数 `KB_POOL_CONFIG` でも指定できます。
ビルダーではCLIの `--pool-config` が `KB_POOL_CONFIG` より優先します。
接続各項目の優先順位は **明示した `--origin` / `--key-file` / `--private-http` →
`KB_POOL_ORIGIN` / `KB_POOL_KEY_FILE` / `KB_POOL_PRIVATE_HTTP` → 共通JSON** です。
Remote KBでは、ラッパーの接続先と一致させるため `KB_POOL_*` を `build_args` より優先します。
`KB_POOL_PRIVATE_HTTP=0` でJSONの `private_http: true` を上書きできます。

共通JSONを使わず直接指定する従来の設定も利用できます。

**初回は各PCで次の設定を行ってください。**

1. 設定ディレクトリを作ります。

   ```bash
   mkdir -p "$HOME/.config/kb"
   chmod 700 "$HOME/.config/kb"
   ```

2. 号池のクライアントAPIキーを `~/.config/kb/client.key` に保存し、
   `chmod 600 "$HOME/.config/kb/client.key"` を実行します。
   ファイルの内容はキー本体だけです。
3. `~/.config/kb/config.json` をエディターで作成し、下の例の
   `https://your-pool.example` を実際の号池URLへ置き換えます。
   URLの末尾には `/v1` や `/_pool/rr` を付けません。
   すでにファイルがある場合は、既存の `stores` を残したまま `build_args` を追加・編集します。

```json
{
  "stores": [],
  "build_args": [
    "--origin", "https://your-pool.example",
    "--key-file", "~/.config/kb/client.key",
    "--workers", "12"
  ]
}
```

その後 `kb register` または `kb store add <名前> <保存先Git URL>` で保存先を登録します。
これらのコマンドが設定するのは保存先の情報で、号池への接続設定は上記で用意します。

Tailscaleなどの確認済み私設トンネル内でHTTPを使う場合は、`--origin` にそのHTTP URLを指定し、
`build_args` の要素として `"--private-http"` も追加してください。

この `config.json` とキーファイルはリポジトリの外に置くため、Gitの管理対象に含まれません。
リポジトリに載せているのは設定例だけです。別のPCで使う際にも、そのPCの接続先とキーを設定します。

読解指示・API指示・共通マップ・二次圧縮オプションも `build_args` で指定できます。
未指定時は作成器の既定値を使います。元KBに使った独自の作成オプションを維持したい場合は、
ここに同じ指定を設定してください。再作成では変更のないファイル群のblobを再利用し、
変更・追加・削除の影響を受けたファイル群だけ圧縮します。空になったパックのblobは取り除きます。
パック対応情報はKBの `info.json` に保存し、旧KBでは一致するローカル作成状態から取得します。
対応情報がない場合と二次圧縮済みKBは全体を作り直します。再利用したblob内の古い構造マップより
現在の構造マップを優先できるよう、新しいマップをKBに添えます。既定は12並列・二次圧縮なしです。

生成時のモデル・推論強度・読解指示は圧縮を作るための設定です。`kb codex` で会話する際の
モデルやベース指示は、そのPCのCodex設定を使います。作成者の設定をKBから復元しません。
ただし、生成時にblobへ圧縮された資料や読解指示の影響は、そのblobに保持されます。

Gitキャッシュと取得済みKBは `~/.cache/kb/`、再作成の中間成果物は
`~/.local/share/kb/builds/` に置きます。

## mapだけ見る

```bash
uv run python repo_map.py --repo /path/to/repository \
  --show-repo-map --map-tokens 10000 --map-multiplier-no-files 1
```

Aiderの同じオプションで呼ばれる解析・ランキング・表示処理を独立して実行します。
`--output repository-map.txt` でファイル出力できます。GitのHEAD/indexのファイルを対象にし、
`.aiderignore` を適用します。キャッシュは `~/.cache/kb-repomap/` に置きます。

10,000トークンはAiderと同じ近似予算です。サンプリングと15%許容幅があるため厳密な上限では
ありません。計数は `cl100k_base`（単体コマンドの `--encoding` で変更可能）。Aider側のモデル・
設定が異なれば出力も変わり得ます。対話用の編集禁止の前置きはmapに含めません。

## リポジトリからKBを作る

号池と共通の接続設定を使う場合:

```bash
uv run python kb_repo_url.py https://github.com/owner/repository.git \
  --name my-kb --pool-config ~/codex-account-pool/bridge.json
```

接続先とキーの場所を直接指定する場合:

```bash
uv run python kb_repo_url.py https://github.com/owner/repository.git \
  --name my-kb \
  --origin https://your-pool.example \
  --key-file /path/to/pool-client.key
```

ローカルのGitリポジトリも指定できます。Tailscale等の確認済み私設トンネル内のHTTPなら:

```bash
uv run python kb_repo_url.py /path/to/repository \
  --name my-kb \
  --origin http://your-tailscale-host:18473 --private-http \
  --key-file /path/to/pool-client.key
```

originには `/v1` などのパスを付けません。宛先はプールの **`/_pool/rr/responses`** です。
一次・二次とも専用のround-robin経路を使います。認証はプールのクライアントAPIキーで行い、
Codexの認証ファイルは読みません。アカウント選択・トークン更新はプールの担当です。

Codex用の `User-Agent` と `originator` も号池側で毎回付与します。
号池はルート別ヘッダー設定に対応した版（`421a868` 以降）を使い、サーバーの
`pool.json` の `round_robin_endpoints["/_pool/rr/responses"].headers` に
実通信で確認した値を設定してください。KB側ではCodexのバージョンや識別ヘッダーを固定しません。
KBが送るのはプール認証と `Content-Type: application/json`、`Accept: text/event-stream` です。
WebSocket専用ヘッダーや一時的なセッションIDは、このHTTP/SSE経路にはコピーしません。

このツール用の `KB_POOL_CONFIG`、`KB_POOL_ORIGIN`、`KB_POOL_KEY_FILE`、`KB_POOL_PRIVATE_HTTP=1` でも指定できます。
Codex側の設定・ログイン・環境変数を変更する処理はありません。

### 作成の流れ

1. 管理用ディレクトリへcloneし、Aiderエンジンで `repository-map.txt` を生成。
2. Git追跡ファイルのうちRepomixの既定フィルタを通ったテキストを候補にし、
   従来のバイナリ・生成物・大きなデータファイル等の除外ルールも適用する。
   `.gitignore`、`.ignore`、`.repomixignore` もRepomix側で反映する。
   Luna等による追加のAI選別は行わない。除外したファイルと理由は作成状態の
   `skipped` に記録する。導入時は `install.sh` がNode依存のRepomixもインストールする。
   残ったソースを関連ディレクトリごと約150K入力トークンずつにまとめる。
   各パックに共通mapと読解指示を付け、userメッセージ1件として直接圧縮へ送る。
3. `input` の末尾に `{"type":"compaction_trigger"}` を追加し、`stream=true, store=false` で送信。
   SSEの `response.output_item.done` から暗号化blobを取得し、`response.completed` と使用量を確認。
4. 二段階圧縮を選んだ場合だけ、指定した個数または一次のAPI報告出力トークン数の合計で
   隣接blobをまとめ、同じv2で再圧縮。既定ではこの工程を省略し、一次blobをそのまま使う。
   最終結果も複数blobになり得る。出力トークン数は二次入力の見積もりであり厳密な上限保証ではない。
5. `kb create` はblob列＋KB用のuser指示をJSON配列として保存する。
   単体ビルダーの従来のセッション鋳造は別機能で、下記を参照。

`compaction`、`compaction_summary`、暗号化内容を持つ `context_compaction` を扱います。
blobの未知のフィールドも保存・再送し、セッション出力でも型名や内容を再構成しません。
旧 `/responses/compact` やテキスト要約へのfallbackはありません。

mapは構造・識別子の抜粋で、従来のLLMによる業務・設計解説とは内容が異なります。
各パックにはmapだけでなく対象ソースの本文も入ります。

### オプションと設定

- `--dry-run`: clone・map生成・パック計画まで。推論API呼び出しなし。
- `--map-only`: clone・map生成まで。
- `--map-tokens 10000`: mapの予算。
- `--repo-map /path/to/map.txt`: 共通mapをファイルで指定する。自作のMarkdown概要なども使える。
- `--refresh-map`: 同じコミットでもmapを再生成。
- `--ref main`: ブランチ・タグ・コミットを指定。
- `--second-stage-count N`: 二次圧縮を有効にし、一次blobをN個ずつまとめる。
- `--second-stage-budget-tokens N`: 二次圧縮を有効にし、一次blobの出力トークン合計N以下でまとめる。
- `--two-stage`: 設定ファイルの方式で二次圧縮を有効にする（既定の方式はトークン合計）。
- `--no-two-stage`: 明示的に二次圧縮を無効にする。方式のオプションより優先。
- `--no-mint`: Codexのセッションファイルを作らずblob生成まで。
- `--prelude-file metadata.md`: 追加資料を別blobとして先頭へ置く。二段階圧縮の対象外。

[config.yaml](config.yaml) の既定値は `gpt-6-sol / medium`、最大12並列、map予算10K、
一次パック予算150K、**二次圧縮なし**です。

通常の作成コマンドに、必要な場合だけ次のどちらかを追加します。

```bash
# 一次blobを4個ずつまとめて二次圧縮
--second-stage-count 4

# 一次blobのAPI報告出力トークン数を、合計150,000以下ずつまとめて二次圧縮
--second-stage-budget-tokens 150000
```

この2つの方式は同時には指定できません。個数は「最終blobの個数」ではなく、
「二次圧縮1回へ入れる一次blobの個数」です。個数方式ではトークン上限による分割を追加しません。
トークン方式は `state.json` の `blob_output_tokens`（一次APIの `usage.output_tokens`）を使い、
暗号化文字列の長さをトークン数に換算しません。単独になったblobは再圧縮せず残します。
preludeと二次圧縮済みblobは、どちらの方式でも対象外です。

設定ファイルでも指定できます。

```yaml
two_stage:
  enabled: false       # trueにすると既定で二次圧縮する
  mode: tokens         # tokens または count
  token_budget: 150000 # mode: tokens のとき
  count: 4             # mode: count のとき
```

既存KBに後から方式を指定して同じコマンドを再実行すれば、完了済み一次パックを再生成せず
二次圧縮できます。無効化オプションは、すでに作成済みの二次blobを一次へ戻す操作ではありません。

旧ツールの再試行・引き直しも引き継ぎます。一時的なHTTP/接続・読み取りエラーは最大6回試行。
`response.completed` より前にストリームが閉じた場合も再試行し、未完了のblobは採用しません。
上流が明示的に失敗を返した場合や、完了した応答に正しいblobがない場合は、この再試行の対象外です。
一次の報告出力が `thin_blob_threshold`（既定2,000）未満なら1回引き直し、二次は2,000未満で
1回引き直します。出力トークン数は品質保証ではありません。一次の引き直しは設定値0で無効化可能。
SSE内のfailed/incompleteや暗号化blob欠落は成功扱いしません。

### KB作成時の共通マップ・指示を変更する

共通マップと2種類の指示を、それぞれUTF-8テキストファイルで指定できます。

```bash
uv run python kb_repo_url.py /path/to/repository \
  --name my-kb-custom \
  --origin https://your-pool.example --key-file /path/to/pool-client.key \
  --repo-map ./map.md \
  --reading-instructions-file ./reading.txt \
  --instructions-file ./instructions.txt
```

- `--repo-map`: 指定ファイルの全文を全パックの `REPOSITORY MAP` 部分へ入れる。
  Aider形式に限定せず、自作の概要・構成説明なども使えます。指定時はAiderによる生成を省略し、
  未指定ならAiderで自動生成します。`--map-tokens` は自動生成時の予算です。
- `--reading-instructions-file`: 各パックの `READING INSTRUCTIONS` の読解指示を置換。
  共通map、ファイル一覧、各ファイルの全文は通常どおり付きます。preludeにも同じ指示を使います。
- `--instructions-file`: 一次・二次圧縮のAPI `instructions` を置換。

各オプションは単独でも併用でも指定できます。2種類の指示は未指定なら既定文を使い、
指定した場合はファイル全文で置換します。
空白・改行は削らず、空ファイルなら指示は空文字になります。再試行・引き直しにも同じ文を使います。
`kb_repo.py` を直接実行する場合も、これらのオプションを使えます。

既定の読解指示:

```text
以下のファイルをすべて全文読んでよく咀嚼し、以後、これらのファイルに対する様々な作業や質問、検索で使える知識として保持してください。
構造マップはあくまで静的解析に基づく構造情報です。実装と矛盾する場合は実装を正とし、推測を事実として扱わないでください。
```

既定のAPI `instructions`:

```text
あなたはKB(知識ベース)である。与えられる文書を全て精読し、知識として保持する。要約を求められたら核心を落とさない。
```

生成時の指示は `state.json` の `reading_instructions` と `instructions` に記録します。
`kb_api.py merge-old` は保存済みのAPI指示を使い、`--instructions-file` で上書きもできます。
既存blobの指示は後から変更できないため、指示を変えて作り直す場合は新しい `--name` を使ってください。
再開時は初回と同じ指示ファイルを指定します。

### 単体ビルダーの保存先・再開・従来のfork

既定は `~/.kb-repomap/`。このツールの `KB_REPOMAP_HOME` で変更できます。
旧 `~/.kb-multiblob/` は使いません。

```text
~/.kb-repomap/
  repos/my-kb/                 管理用clone
  my-kb/repository-map.txt     構造マップ
  my-kb/state.json             blob・完了パック・使用量
  kb-current                  現在のKB名
  kb-session                  fork元セッションID
```

完了済みの同一パックは再実行で省略します。古い内容を置き換える同期機構ではないため、
別リビジョンのKBは新しい `--name` で作成してください。
途中失敗から再開する場合は、同じ `KB_REPOMAP_HOME`・ソースcommit・map・作成オプションで
ビルダーを再実行します。`kb create` や `kb codex` の再作成を改めて選ぶと、新しい作業ディレクトリを
作るため、前回の途中保存を自動では引き継ぎません。

以下は単体の `kb_fork_mint.py` による従来のセッション鋳造です。
`kb create` / `kb codex` の通常経路では使いません。
このfork元生成は既存Codexセッションの `session_meta` をテンプレートとして読み、
新しい `~/.codex/sessions/.../rollout-*.jsonl` を書きます。初回は既存セッションが必要です。

**複数の独立したcompaction blobを同じ履歴に並べることが、このKBの意図した仕様です。**
セッション生成時に1個へ統合したり、最後のblobだけを残したりしません。
二次圧縮は明示的に選択した場合だけ実行します。
`session_meta.history_mode: legacy` はJSONL履歴の保存・読み込み方式であり、
圧縮APIのv2とは別です。各 `response_item` にはv2で返った暗号化blobをそのまま入れます。

```bash
codex fork "$(cat ~/.kb-repomap/kb-session)"
```

## 検証

```bash
uv run python -m unittest discover -s tests -v
uv run python scripts/check-aider-parity.py /path/to/pinned-aider /path/to/repository

# 任意: 合成ソースで実APIを4回呼び、二段階圧縮後の値を照会
uv run python scripts/live-check.py \
  --origin https://your-pool.example --key-file /path/to/pool-client.key \
  --output state/live-check.json
```

結果と検証範囲は [docs/validation.md](docs/validation.md) に記載しています。

## 出典・ライセンス

KB処理はMITの `yukimaru77/kb-multiblob` が元です。Aider部分はApache-2.0です。
[PROVENANCE.md](PROVENANCE.md) と [Aiderの出典](vendor/aider_repomap/PROVENANCE.md) に
元コミット・対象ファイル・変更点を記録しています。
