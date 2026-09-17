# kb-repomap

Gitリポジトリを **Aiderの構造マップ → v2圧縮blob → Codexのfork元セッション** にするツールです。

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

## 基本の4コマンド

```bash
bash install.sh
kb register xx   # 対話で登録。kb 登録 xx でも同じ
kb create xx     # 基準ブランチからKBを作成し、保存先へcommit/push
kb list          # 登録済みKBと作成状態を一覧表示
kb codex xx      # KB入りの新規Codexセッションを開く
```

`kb register` は次を質問します。KB名を引数で渡した場合、名前の質問は省略します。

1. KB名
2. 元リポジトリのURL
3. 基準ブランチ（既定: `main`）
4. 保存先GitリポジトリのURL（登録済みの保存先があれば表示）

保存先には既存のGitリポジトリを指定します。新しいURLならローカルの保存先一覧にも登録し、
同じURLを登録済みならその設定を使います。登録時は `<KB名>/info.json` を保存し、
`source_commit` は `null`（未作成）になります。登録だけでは推論APIを呼びません。
既存の同名KBへの再登録は上書きせずエラーになります。

`kb create xx` は登録されたURL・ブランチから、その時点のHEADを固定して作成します。
成功すると `info.json` に作成commitを記録し、`latest.jsonl` と一緒にcommit/pushします。
作成済みKBに実行すると作り直します。登録した保存先を指定する場合は
`kb create xx --store <保存先名>` を使います。

作成には下記の `build_args` または `KB_POOL_*` による圧縮API接続先の設定が必要です。
`kb list` では未作成KBを「未作成」と表示します。作成後は同じ名前で `kb codex xx` を使えます。

## Gitに保存したKBを `kb codex` で開く

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
`kb codex octane --store work` で保存先を指定できます。配置は次の2ファイルです。

```text
octane/
  info.json
  latest.jsonl
```

`info.json`:

```json
{
  "repository_url": "https://github.com/owner/repository.git",
  "source_commit": "KBを作成したcommitの完全なSHA",
  "branch": "main"
}
```

名前はディレクトリ名、session IDはJSONL先頭の `session_meta.id` から取得します。
`latest.jsonl` の複数blob・型・未知のフィールドはそのまま保存します。
新しいKBを保存すると2ファイルを同じcommitで更新し、過去版はGit履歴に残ります。

起動時の流れ:

1. 保存先Gitリポジトリをfetchし、同じcommitにある `info.json` と `latest.jsonl` を取得。
2. 元リポジトリの基準ブランチをfetchし、KB作成時commitと比較。
3. commitが異なる場合に「KBを作り直しますか？ [y/N]」と質問。
   - **Yes**: 既存のrepo-map/v2圧縮処理でその時点のHEADから作り直し、選択された保存先にcommit/push。
   - **No**: 既存KBに加えてcommit一覧とGit diffを使う。保存済みKBは更新しない。
4. 取得したJSONLのsession IDをローカル用の新IDにしたコピーを取り込み、そこからforkする。
   既存セッションとのID衝突を避け、blobを含む履歴行はそのまま維持する。
   更新差分を**fork後の最初の依頼**に渡す。
   元テンプレート末尾へ未完了メッセージとして追記する方法は使わない。
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

下記の作成コマンドが出力したJSONLを指定します。

```bash
kb publish octane --store work \
  --repository-url https://github.com/owner/repository.git \
  --source-commit <完全なSHA> --branch main \
  --jsonl /path/to/rollout-....jsonl
```

### 作成・再作成時の接続先とオプション

このツールの設定は `~/.config/kb/config.json` に保存します。`stores` は `kb store add` が管理します。
`kb create` と起動時の再作成で使用するビルダーの引数は、同ファイルの `build_args` で指定します。
キー本体は設定や保存先リポジトリに入れず、ローカルのキーファイルを指定してください。

```json
{
  "stores": [{"name": "work", "url": "https://github.com/your-account/kb-store.git"}],
  "build_args": [
    "--origin", "https://your-pool.example",
    "--key-file", "/path/to/pool-client.key",
    "--workers", "12"
  ]
}
```

読解指示・API指示・共通マップ・二次圧縮オプションも `build_args` で指定できます。
未指定時は作成器の既定値を使います。元KBに使った独自の作成オプションを維持したい場合は、
ここに同じ指定を設定してください。再作成は新しいstateディレクトリを使うため、
古いcommitの完了済みパックを混ぜません。既定は12並列・二次圧縮なしです。

Gitキャッシュと取得済みJSONLは `~/.cache/kb/`、再作成の中間成果物は
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

このツール用の `KB_POOL_ORIGIN`、`KB_POOL_KEY_FILE`、`KB_POOL_PRIVATE_HTTP=1` でも指定できます。
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
5. セッション情報＋blob列＋KB用の指示を、新しいCodexセッションJSONLとして書く。

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

[config.yaml](config.yaml) の既定値は `gpt-5.6-sol / high`、最大12並列、map予算10K、
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

### 保存先・再開・fork

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

fork元生成は既存Codexセッションの `session_meta` をテンプレートとして読み、
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
