#!/usr/bin/env python3
"""Build repository knowledge with native Responses compaction-trigger/v2."""
import argparse, glob, ipaddress, json, os, random, ssl, sys, time
from pathlib import Path
import urllib.request, urllib.error
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead

COMPACTION_TYPES = {"compaction", "compaction_summary", "context_compaction"}
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"
DEFAULT_COMPACT_WORKERS = 12
COMPACT_MAX_ATTEMPTS = 6
COMPACT_RETRY_MAX_DELAY = 30
COMPACT_RETRYABLE_HTTP_CODES = {408, 409, 429, 500, 502, 503, 504}
STATE_ROOT = os.path.expanduser(os.environ.get("KB_REPOMAP_HOME", "~/.kb-repomap"))
CHARTER = ("あなたはKB(知識ベース)である。与えられる文書を全て精読し、知識として保持する。"
           "要約を求められたら核心を落とさない。")
INTRO = "今からあなたに文書群を渡すので、全て精読してください。"

def pool_configuration(environ=None):
    environ = os.environ if environ is None else environ
    origin = environ.get("KB_POOL_ORIGIN", "").rstrip("/")
    key_file = environ.get("KB_POOL_KEY_FILE", "")
    if not origin or not key_file:
        raise ValueError("set KB_POOL_ORIGIN and KB_POOL_KEY_FILE, or use --origin and --key-file")
    parsed = urlparse(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("pool origin must be http(s), without a path or embedded credentials")
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if parsed.scheme == "http" and not local and environ.get("KB_POOL_PRIVATE_HTTP") != "1":
        raise ValueError("remote HTTP requires --private-http for a verified private tunnel")
    key = Path(key_file).expanduser().read_text().strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("pool key file must contain a nonempty single token")
    return origin + "/_pool/rr", key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(path, body, timeout=600, stream=False):
    base_url, api_key = pool_configuration()
    request = urllib.request.Request(
        base_url + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json",
                 "Originator": "codex_cli_rs"},
        method="POST",
    )
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def events(source):
    """Read SSE independently of Content-Type; support multiline data and CRLF."""
    data = []
    for raw in source:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                data = []
                if payload != "[DONE]":
                    yield json.loads(payload)
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def compaction_result(source):
    items = []
    for event in events(source):
        kind = event.get("type")
        if kind in {"error", "response.failed", "response.incomplete"}:
            raise ValueError(f"upstream returned {kind}")
        if kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") in COMPACTION_TYPES:
                items.append(item)
        if kind == "response.completed":
            if (len(items) != 1 or not isinstance(items[0].get("encrypted_content"), str)
                    or not items[0]["encrypted_content"]):
                raise ValueError("upstream completed without one encrypted compaction item")
            return items, (event.get("response") or {}).get("usage") or {}
    raise ValueError("stream ended before response.completed")


def u(text):  # user message item
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
def a(text):  # 合成assistant message item
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
    def est_tokens(s): return len(_ENC.encode(s, disallowed_special=()))
except Exception:
    _ENC = None
    def est_tokens(s): return int(len(s) / 3.3)  # fallback概算
def items_tokens(items):
    return est_tokens(json.dumps(items, ensure_ascii=False))

def record_blob_output_tokens(state, blobs, usage):
    """Allocate one compact call's measured output tokens across returned blobs.

    The API reports usage for the response as a whole. It normally returns one
    compaction_summary; if it returns several, allocate proportionally by encrypted
    payload length while preserving the measured total exactly.
    """
    total = usage.get("output_tokens")
    identified = [blob for blob in blobs if blob.get("id")]
    if not isinstance(total, int) or total < 0 or not identified:
        return
    weights = [max(1, len(blob.get("encrypted_content", ""))) for blob in identified]
    weight_total = sum(weights)
    allocations = [total * weight // weight_total for weight in weights]
    for index in range(total - sum(allocations)):
        allocations[index % len(allocations)] += 1
    token_map = state.setdefault("blob_output_tokens", {})
    for blob, allocated in zip(identified, allocations):
        token_map[blob["id"]] = allocated

def current_blob_output_tokens(state):
    blobs = [
        blob for blob in state.get("blobs") or state.get("base_items", [])
        if blob.get("type") in COMPACTION_TYPES
    ]
    token_map = state.get("blob_output_tokens") or {}
    ids = [blob.get("id") for blob in blobs]
    if not ids or any(not blob_id or blob_id not in token_map for blob_id in ids):
        return None
    return sum(token_map[blob_id] for blob_id in ids)

def state_path(name): return os.path.join(STATE_ROOT, name, "state.json")
def load_state(name):
    p = state_path(name)
    if os.path.exists(p): return json.load(open(p))
    return {"base_items": [u(INTRO), a("精読します。")], "fed": [], "rounds": []}
def save_state(name, st):
    os.makedirs(os.path.dirname(state_path(name)), exist_ok=True)
    json.dump(st, open(state_path(name), "w"), ensure_ascii=False)

def compact_retry_delay(attempt, error):
    """Return bounded exponential backoff, honoring numeric Retry-After."""
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(COMPACT_RETRY_MAX_DELAY, max(0, float(retry_after)))
        except ValueError:
            pass
    base = min(COMPACT_RETRY_MAX_DELAY, 2 ** attempt)
    return min(COMPACT_RETRY_MAX_DELAY, base + random.random() * base)


def compact_error_summary(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    if isinstance(error, json.JSONDecodeError):
        return "invalid JSON response"
    if isinstance(error, IncompleteRead):
        return "incomplete response body"
    return str(error) or error.__class__.__name__


def compact(items, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT,
            retry_label="compact"):
    body = {"model": model, "input": [*items, {"type": "compaction_trigger"}], "instructions": CHARTER,
            "stream": True, "store": False,
            "reasoning": {"effort": effort},  # 深い思考で要約(保持量が増える実測傾向)
            "parallel_tool_calls": False}
    t0 = time.time()
    for attempt in range(COMPACT_MAX_ATTEMPTS):
        try:
            with http("/responses", body, stream=True) as r:
                output, usage = compaction_result(r)
            break
        except urllib.error.HTTPError as error:
            if error.code == 401:
                error.close()
                raise RuntimeError("pool authentication rejected (401); check its client key and account pool")
            retryable = error.code in COMPACT_RETRYABLE_HTTP_CODES
            if not retryable or attempt == COMPACT_MAX_ATTEMPTS - 1:
                error.close()
                raise
            delay = compact_retry_delay(attempt, error)
            retry_summary = compact_error_summary(error)
            error.close()
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            IncompleteRead,
            json.JSONDecodeError,
            ssl.SSLError,
        ) as error:
            if attempt == COMPACT_MAX_ATTEMPTS - 1:
                raise
            delay = compact_retry_delay(attempt, error)
            retry_summary = compact_error_summary(error)
        print(
            f"retry {retry_label}: {retry_summary} "
            f"(attempt {attempt + 2}/{COMPACT_MAX_ATTEMPTS}) in {delay:.1f}s",
            file=sys.stderr,
        )
        time.sleep(delay)
    return output, round(time.time() - t0, 1), usage

def summarize_items(items):
    kinds = []
    for it in items:
        t = it.get("type")
        if t in COMPACTION_TYPES: kinds.append(f"compaction({len(it.get('encrypted_content',''))}c)")
        elif t == "message":
            txt = "".join(c.get("text", "")[:30] for c in it.get("content", []) if isinstance(c, dict))
            kinds.append(f"msg/{it.get('role')}:{txt[:30]!r}")
        else: kinds.append(t)
    return kinds

def cmd_build(name, files, per_compact, budget, multi_blob=False):
    st = load_state(name)
    fed = set(st["fed"])
    todo = [f for f in files if os.path.basename(f) not in fed]
    print(f"KB[{name}] 既読{len(fed)}本 / 今回対象{len(todo)}本 / X={per_compact} / budget={budget//1000}K")
    batch = []
    def pending_tokens():
        base = [u(INTRO)] if multi_blob else list(st["base_items"])
        items = base + [u(open(f, errors='replace').read()) for f in batch]
        return items_tokens(items)
    def fire():
        nonlocal batch
        if not batch: return
        # multi_blob: 圧縮入力は「導入+新規論文のみ」— 過去ブロブは絶対に再圧縮させない
        items = [u(INTRO)] if multi_blob else list(st["base_items"])
        for fp in batch:
            body = open(fp, errors="replace").read()
            items.append(u(f"=====DOC: {os.path.basename(fp)}=====\n\n{body}"))
            items.append(a("精読完了しました。"))
        tok = items_tokens(items)
        out, secs, usage = compact(
            items, retry_label=f"build round {len(st['rounds']) + 1}"
        )
        rd = {"n": len(st["rounds"]) + 1, "papers": [os.path.basename(b) for b in batch],
              "input_tok_exact": tok, "secs": secs, "usage": usage, "out": summarize_items(out)}
        st["rounds"].append(rd)
        if multi_blob:
            new_blobs = [i for i in out if i.get("type") in COMPACTION_TYPES]
            st.setdefault("blobs", []).extend(new_blobs)
            record_blob_output_tokens(st, new_blobs, usage)
            st["base_items"] = st["blobs"]  # ask互換: baseは蓄積ブロブ列
        else:
            st["base_items"] = out
        st["fed"] += [os.path.basename(b) for b in batch]
        save_state(name, st)
        mu = usage.get("input_tokens"); cu = (usage.get("input_tokens_details") or {}).get("cached_tokens")
        print(f"round#{rd['n']}: {len(batch)}本 送信厳密{tok//1000}K / API実測 in={mu} (cached={cu}) "
              f"out={usage.get('output_tokens')} / {secs}s")
        batch = []
    for fp in todo:
        batch.append(fp)
        # 発火はトークン閾値主導(厳密カウント)。per_compactは上限オプション
        if pending_tokens() >= budget or (per_compact and len(batch) >= per_compact):
            fire()
    fire()
    print(f"完了: {len(st['fed'])}本 / {len(st['rounds'])}圧縮 / base_items={len(st['base_items'])}")

def plan_token_groups(blobs, token_map, budget_tokens):
    """Greedily group adjacent blobs without exceeding measured output tokens."""
    if budget_tokens < 1:
        raise SystemExit("budget tokens must be at least 1")
    groups = []
    current = []
    current_tokens = 0
    start = 0
    for index, blob in enumerate(blobs):
        blob_id = blob.get("id")
        tokens = token_map.get(blob_id)
        if not blob_id or not isinstance(tokens, int) or tokens < 0:
            raise SystemExit(
                f"first-stage blob {blob_id or '<missing-id>'} has no measured output token count"
            )
        if current and current_tokens + tokens > budget_tokens:
            groups.append((start, index - 1, current, current_tokens))
            current = []
            current_tokens = 0
            start = index
        current.append(blob)
        current_tokens += tokens
    if current:
        groups.append((start, len(blobs) - 1, current, current_tokens))
    return groups


def plan_count_groups(blobs, token_map, blob_count):
    """Group adjacent blobs by count; token measurements are only for reporting."""
    if blob_count < 1:
        raise SystemExit("blob count must be at least 1")
    groups = []
    for start in range(0, len(blobs), blob_count):
        group = blobs[start:start + blob_count]
        measured = [token_map.get(blob.get("id")) for blob in group]
        tokens = sum(measured) if all(isinstance(t, int) and t >= 0 for t in measured) else None
        groups.append((start, start + len(group) - 1, group, tokens))
    return groups


def cmd_merge_old(name, keep_recent, budget_tokens=150_000, model=DEFAULT_MODEL,
                  effort=DEFAULT_EFFORT, workers=DEFAULT_COMPACT_WORKERS,
                  blob_count=None):
    """2段階ブロブ制: 個数、または実測output token合計で生ブロブを畳む(2段目)。
    鉄則: 統合済みブロブ(state['stage2_ids']に記録)は二度と再統合しない — 圧縮は
    どの知識も生涯2回まで(3回目からは知識が溶ける: KB-v1の実証)。単独groupだけは
    無意味な再圧縮を避けて生のまま残す。実質量は各roundのAPI output_tokensを使い、
    encrypted_contentのtiktoken計測は使わない。keep_recentは廃止(無視)。"""
    if blob_count is not None and blob_count < 1:
        raise SystemExit("blob count must be at least 1")
    if blob_count is None and budget_tokens < 1:
        raise SystemExit("budget tokens must be at least 1")
    if workers < 1:
        raise SystemExit("workers must be at least 1")
    st = load_state(name)
    blobs = [b for b in st.get("blobs") or st["base_items"] if b.get("type") in COMPACTION_TYPES]
    s2 = set(st.get("stage2_ids") or [])
    prelude_ids = set(st.get("prelude_blob_ids") or [])
    preludes = [b for b in blobs if b.get("id") in prelude_ids]
    done = [b for b in blobs if b.get("id") in s2 and b.get("id") not in prelude_ids]
    raw = [b for b in blobs if b.get("id") not in s2 and b.get("id") not in prelude_ids]
    st.setdefault("stage1_blob_count", len(blobs))
    stage1_tokens = current_blob_output_tokens(st)
    if stage1_tokens is not None:
        st.setdefault("stage1_blob_output_tokens", stage1_tokens)
    if len(raw) < 2:
        st["blobs"] = preludes + done + raw
        st["base_items"] = st["blobs"]
        save_state(name, st)
        print(f"生ブロブ{len(raw)}個 — 畳むものなし(統合済み{len(done)}個)"); return
    token_map = st.get("blob_output_tokens") or {}
    if blob_count is None:
        planned_groups = plan_token_groups(raw, token_map, budget_tokens)
        group_description = f"実測合計{budget_tokens}tok以下"
    else:
        planned_groups = plan_count_groups(raw, token_map, blob_count)
        group_description = f"{blob_count}個ずつ"
    work = [
        (start, end, group, tokens)
        for start, end, group, tokens in planned_groups
        if len(group) >= 2
    ]
    if not work:
        st["blobs"] = preludes + done + raw
        st["base_items"] = st["blobs"]
        save_state(name, st)
        print(
            f"生ブロブ{len(raw)}個はすべて単独group — 畳むものなし"
            f"({group_description}、統合済み{len(done)}個)"
        )
        return
    import shutil
    shutil.copy(state_path(name), state_path(name) + ".pre-merge")
    merge_blob_count = sum(len(group) for _start, _end, group, _tokens in work)
    print(
        f"生{len(raw)}個のうち{merge_blob_count}個を{group_description}・"
        f"最大{min(workers, len(work))}並列で畳む(統合済み{len(done)}個は不可侵)"
    )

    def merge_group(task):
        start, end, grp, input_blob_tokens = task
        items = [u("以下は複数の圧縮済み知識ブロブである。含まれる論文知識を全て保持したまま統合せよ。")] + grp
        measured = f"{input_blob_tokens}tok" if input_blob_tokens is not None else "tokens unknown"
        merge_label = f"merge raw {start}..{end} ({measured})"
        out, secs, usage = compact(
            items, model=model, effort=effort, retry_label=merge_label
        )
        news = [x for x in out if x.get("type") in COMPACTION_TYPES]
        if (usage.get("output_tokens") or 0) < 2000:  # 痩せブロブは1回引き直し(非決定性)
            out2, secs2, usage2 = compact(
                items, model=model, effort=effort,
                retry_label=f"{merge_label} thin-redraw",
            )
            news2 = [x for x in out2 if x.get("type") in COMPACTION_TYPES]
            if (usage2.get("output_tokens") or 0) > (usage.get("output_tokens") or 0):
                news, usage, secs = news2, usage2, secs2
        if not news:
            raise RuntimeError(f"no compaction_summary returned for {merge_label}")
        return start, end, input_blob_tokens, news, secs, usage

    merged_by_start = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(work))) as executor:
        results = executor.map(merge_group, work)
        for start, end, input_blob_tokens, news, secs, usage in results:
            merged_by_start[start] = news
            record_blob_output_tokens(st, news, usage)
            s2.update(x.get("id", "") for x in news)
            st["rounds"].append({
                "n": len(st["rounds"]) + 1,
                "papers": [f"<merge raw {start}..{end}>"],
                "input_blob_tokens": input_blob_tokens,
                "secs": secs,
                "usage": usage,
                "out": summarize_items(news),
            })
            print(
                f"  {start}..{end} → 1個 "
                f"(out={usage.get('output_tokens')}tok / {secs}s)"
            )

    merged = []
    leftovers = []
    for start, end, group, _tokens in planned_groups:
        if start in merged_by_start:
            merged.extend(merged_by_start[start])
        else:
            merged.extend(group)
            leftovers.extend(group)
    if leftovers:
        print(f"  単独group {len(leftovers)}個は生のまま保持")
    # Prelude blobs stay oldest/first and are never recompressed with source blobs.
    st["blobs"] = preludes + done + merged
    st["base_items"] = st["blobs"]
    st["stage2_ids"] = sorted(s2)
    st["stage2_blob_count"] = len(st["blobs"])
    stage2_tokens = current_blob_output_tokens(st)
    if stage2_tokens is not None:
        st["stage2_blob_output_tokens"] = stage2_tokens
    save_state(name, st)
    new_merged_count = sum(len(news) for news in merged_by_start.values())
    print(f"完了: 総ブロブ{len(st['blobs'])}個(統合済み{len(done)+new_merged_count}+生{len(leftovers)})。fork元の再鋳造(kb-fork-mint.py)を忘れないこと")

def cmd_ask(name, question, blob_only=False):
    st = load_state(name)
    base = ([it for it in st["base_items"] if it.get("type") in COMPACTION_TYPES]
            if blob_only else st["base_items"])
    body = {"model": "gpt-5.6-sol", "instructions": CHARTER,
            "input": base + [u(question)],
            "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
            "prompt_cache_key": f"kb-{name}",  # 同一ブロブ接頭辞のキャッシュ経路を安定化(2回目以降が高速)
            "store": False, "stream": True}
    txt = []
    with http("/responses", body, stream=True) as r:
        for raw in r:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"): continue
            data = line[5:].strip()
            if data == "[DONE]": break
            try: ev = json.loads(data)
            except Exception: continue
            if ev.get("type") == "response.output_text.delta":
                txt.append(ev.get("delta", ""))
            elif ev.get("type") == "response.output_item.done":
                item = ev.get("item") or {}
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text" and c.get("text") and not txt:
                            txt.append(c["text"])
    print("".join(txt) or "(応答テキストなし)")

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--name", required=True); b.add_argument("--files", nargs="+", required=True)
    b.add_argument("--per-compact", type=int, default=None,
                   help="1圧縮あたりの論文数上限(省略時はトークン閾値のみで発火)")
    b.add_argument("--budget-tokens", type=int, default=190_000)
    b.add_argument("--multi-blob", action="store_true",
                   help="再圧縮しない多ブロブ制: 各論文は生涯1回だけ圧縮され、ブロブを横に蓄積")
    q = sub.add_parser("ask"); q.add_argument("--name", required=True); q.add_argument("question")
    q.add_argument("--blob-only", action="store_true", help="ブロブのみ渡す(生テキストのリーク排除)")
    mg = sub.add_parser("merge-old", help="容量衛生: 旧層ブロブの2段マージ")
    mg.add_argument("--name", required=True)
    mg.add_argument("--keep-recent", type=int, default=20, help="生のまま温存する直近ブロブ数")
    grouping = mg.add_mutually_exclusive_group()
    grouping.add_argument("--budget-tokens", type=int, default=150_000,
                    help="二次圧縮1回へ詰める一次blobの実測token上限")
    grouping.add_argument("--blob-count", type=int,
                    help="二次圧縮1回へ詰める一次blobの個数")
    mg.add_argument("--workers", type=int, default=DEFAULT_COMPACT_WORKERS,
                    help="同時compact数")
    mg.add_argument("--model", default=DEFAULT_MODEL)
    mg.add_argument("--effort", choices=("none", "minimal", "low", "medium", "high", "xhigh"),
                    default=DEFAULT_EFFORT)
    s = sub.add_parser("status"); s.add_argument("--name", required=True)
    ar = ap.parse_args()
    if ar.cmd == "build":
        files = []
        for pat in ar.files: files.extend(sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat])
        cmd_build(ar.name, files, ar.per_compact, ar.budget_tokens, ar.multi_blob)
    elif ar.cmd == "ask":
        cmd_ask(ar.name, ar.question, ar.blob_only)
    elif ar.cmd == "merge-old":
        cmd_merge_old(
            ar.name, ar.keep_recent, ar.budget_tokens, ar.model, ar.effort, ar.workers,
            blob_count=ar.blob_count,
        )
    else:
        st = load_state(ar.name)
        print(json.dumps({"fed": len(st["fed"]), "rounds": len(st["rounds"]),
                          "base": summarize_items(st["base_items"])}, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
