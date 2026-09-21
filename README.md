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
```

Python 3.12と依存関係をプロジェクト専用環境へ用意します。Aider本体・LiteLLM・Codex CLIは
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

作成には下記の `build_args` または `KB_POOL_*` による圧縮API接続先の設定が必要です。
`kb list` では未作成KBを「未作成」と表示します。作成後は同じ名前で `kb codex xx` を使えます。

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
再作成を選んだ場合は同じファイル名を更新します。

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

`--file v1` で名前付き保存も可能です。保存するのは `latest.json`（blob列）と
`info.json`（論文種別・元Git URL・コミット・ブランチ・論文サブディレクトリ・入力SHA256・引用数・本論文blob数・生成設定）です。
接続先キー・アカウント一覧・作成者のセッション設定は保存しません。
論文KBも元Gitリポジトリを必須とし、保存時に指定コミットを取得できることを確認します。
起動時は基準ブランチの更新を確認し、更新があれば再作成方法を表示します。
本文・図を読み直す必要があるため、コード用の差分追加で論文KBを自動更新しません。
再作成は `paper-kb` で行い、再度publishします。`kb create` や
`kb codex --rebuild always` は論文KBでは使用しません。


### 号池から推論時だけKBを挿入する

新しい起動構文は `kb <KB名> [KBオプション] codex [Codexの引数...]` です。
`codex` より後はCodex自身が解釈します。KB用の `--remote`・`--store`・`--file`・
`--rebuild` は `codex` より前に置きます。

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

新構文の `--remote` は、号池にKBの親bindingを登録し、起動するCodexプロセスだけに
親IDのヘッダーを設定します。Codexが作った本来のセッションへ号池がKBを継承・保存するため、
`exec` を `exec resume` に変換せず実行します。最初の余分な推論もありません。
通常は号池のfill-first経路、`pool-rr` 付きならRR経路を使用します。
モデル一覧は通常のCodexが保存した `~/.codex/models_cache.json`（`CODEX_HOME`対応）を使います。
起動時のKB診断はstderrへ出し、stdout・stdin・終了コードはCodexのままです。
Codexの永続設定・ログイン状態は変更しません。provider自体を別サービスへ変える
`-c model_provider=...` などとは併用しないでください。既存セッションに別のKBが
登録済みの場合、その既存bindingが優先されます。

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

`--remote` は、KBを号池に登録して新しいネイティブCodexセッションへ紐付ける。
KB本体をローカルのCodex履歴に入れず、号池が推論要求の先頭のsystem/developer項目の後へ挿入する。
会話のcompaction blobがある場合も、その前に置く。コンパクト要求にはKBを含めない。
サブエージェントは通信上の親セッション情報を通して同じKBを継承する。

登録するのは保存されたKB項目の配列。複数の独立blobとKB憲章の順序を保つ。
起動時にその内容を固定するため、あとで `latest.json` を更新しても稼働中のセッションは変わらない。
Gitの更新確認・再作成の質問・再作成しない場合の差分追加は、通常の `kb codex` と共通。
`--app`、`--session-only`、`--prompt`、`--file` も併用できる。

接続には既存の `~/.config/kb/config.json` の `build_args` にある `--pool-config` で
号池の共通JSON（例: `codex-account-pool/bridge.json`）を指定できる。
既存の `--origin`、`--key-file`、必要なら `--private-http` と `KB_POOL_*` も使える。
`pool-rr` が渡す `KB_POOL_ORIGIN`、`KB_POOL_KEY_FILE`、`KB_POOL_PRIVATE_HTTP` は保存済みの指定より優先する。
Codexの環境変数を設定することはない。
新しい接続設定は不要。**Macの透過ブリッジが同じ号池へ接続していること**と、
Remote KB対応版の `codex-account-pool` が必要。登録エラーの場合、最初のターンは送信しない。

起動直後の先行WebSocket接続を残さないため、空セッションの作成・登録・保存後にapp-serverを
一度終了し、同じセッションを再開して最初のターンを送る。Codexの設定・認証・バイナリは変更しない。
号池側の詳細: `https://github.com/yukimaru77/codex-account-pool/blob/main/docs/remote-kb.md`

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
   更新差分を**新しいセッションの最初の依頼**に渡す。
5. 応答を待ち、そのセッションを `codex resume` で開く。

起動時に推論が1回発生します。基準ブランチと同じcommitなら質問は出ません。
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
ここに同じ指定を設定してください。再作成は新しいstateディレクトリを使うため、
古いcommitの完了済みパックを混ぜません。既定は12並列・二次圧縮なしです。

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
2. 関連ディレクトリのソースを約150K入力トークンずつにまとめる。
   各パックに共通mapと読解指示を付け、userメッセージ1件として直接圧縮へ送る。
   従来のバイナリ・生成物・大きなデータファイル等の除外ルールを引き継ぐ。
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

[config.yaml](config.yaml) の既定値は `gpt-6-astra / low`、最大12並列、map予算10K、
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
