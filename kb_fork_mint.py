#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb-fork-mint — 最新のKB state(多ブロブ)からcodexのfork元セッションを鋳造する。

  合成rollout = session_meta + 全ブロブ(compaction_summary) + KB憲章(user) のみの
  クリーンな土台を ~/.codex/sessions/ に書き、そのidを kb-session に記録する。
  以後 spawn系(team2.sh等)の `codex fork $(cat kb-session)` はこの土台から
  KB入り実務員を起こす。KB追記のたびに再鋳造してよい(稼働中forkは張り替えない)。

  使い方:
    kb_fork_mint.py [--name KB-v2-multiblob] [--no-register]  # 鋳造+kb-session更新
    kb_fork_mint.py --overlay-session <uuid>  # 個体オーバーレイ鋳造(下記)

  個体オーバーレイ鋳造(2026-07-21): --overlay-session に個体のセッションidを渡すと、
  その個体を /compact した後のrolloutから圧縮ブロブ(compaction_summary)を抽出し、
  共有KBブロブ列の**上に重ねた**専用fork元を鋳造する。共有KB(state.json)は汚さない。
  用途: 副司令塔の再誕 — 文献全部(KB)+本人の調査文脈(個体ブロブ)を両方持つ頭を作る。
  オーバーレイ鋳造は kb-session を更新しない(共有fork元にしてはならないため)。
"""
import argparse, json, os, time, uuid, glob
from kb_api import COMPACTION_TYPES

KB_HOME = os.path.expanduser(os.environ.get("KB_REPOMAP_HOME", "~/.kb-repomap"))
CHARTER = ("[KB憲章] あなたはKB(知識ベース)である。頭に入っている大量の文書知識を土台に、"
           "この後与えられる役割(ロールカード)に従って働く。知識は記憶から引く。")

def uuid7():
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().bytes
    b = ms.to_bytes(6, "big") + rand[6:]
    b = b[:6] + bytes([(0x70 | (b[6] & 0x0F))]) + b[7:]
    b = b[:8] + bytes([(0x80 | (b[8] & 0x3F))]) + b[9:]
    return str(uuid.UUID(bytes=b))

def template_meta():
    """既存の実rolloutからsession_meta(base_instructions込み)を流用する"""
    cands = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    for p in cands:
        try:
            first = json.loads(open(p, errors="replace").readline())
        except Exception:
            continue
        if first.get("type") == "session_meta" and (first.get("payload") or {}).get("base_instructions"):
            return first["payload"]
    raise SystemExit("session_metaテンプレートが見つからない(codexを一度でも使った環境が必要)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=None, help="KB名(省略時はkb-currentから)")
    ap.add_argument("--no-register", action="store_true", help="kb-sessionを更新しない")
    ap.add_argument("--overlay-session", default=None,
                    help="個体のセッションid — そのrolloutの圧縮ブロブをKBの上に重ねる(kb-session非更新)")
    a = ap.parse_args()
    name = a.name or open(os.path.join(KB_HOME, "kb-current")).read().strip()
    st = json.load(open(os.path.join(KB_HOME, name, "state.json")))
    blobs = [b for b in st.get("blobs") or st["base_items"] if b.get("type") in COMPACTION_TYPES]
    if not blobs:
        raise SystemExit(f"KB[{name}]にブロブが無い")

    overlay = []
    if a.overlay_session:
        hits = sorted(glob.glob(os.path.expanduser(
            f"~/.codex/sessions/*/*/*/rollout-*{a.overlay_session}*.jsonl")), key=os.path.getmtime)
        if not hits:
            raise SystemExit(f"overlay対象のrolloutが見つからない: {a.overlay_session}")
        for line in open(hits[-1], errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload") or {}
            if d.get("type") == "response_item" and p.get("type") in COMPACTION_TYPES \
                    and p.get("encrypted_content"):
                overlay.append(p)
        if not overlay:
            raise SystemExit(f"rolloutに圧縮ブロブが無い — 先に本人へ /compact を実行させること: {hits[-1]}")
        a.no_register = True  # 個体入りfork元を共有元にしない

    sid = uuid7()
    now = time.gmtime()
    ts = time.strftime("%Y-%m-%dT%H-%M-%S", now)
    meta = dict(template_meta())
    meta["session_id"] = sid; meta["id"] = sid
    meta["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", now) + ".000Z"
    meta["cwd"] = os.getcwd()
    meta["history_mode"] = "legacy"
    for inherited_key in ("forked_from_id", "history_base", "context_window"):
        meta.pop(inherited_key, None)

    outdir = os.path.expanduser(time.strftime("~/.codex/sessions/%Y/%m/%d", now))
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"rollout-{ts}-{sid}.jsonl")
    tsl = meta["timestamp"]
    with open(path, "w") as f:
        ordinal = 0

        def write_rollout_item(item_type, payload):
            nonlocal ordinal
            f.write(json.dumps({"timestamp": tsl, "type": item_type,
                                "payload": payload, "ordinal": ordinal},
                               ensure_ascii=False) + "\n")
            ordinal += 1

        write_rollout_item("session_meta", meta)
        for b in blobs:
            write_rollout_item("response_item", b)
        for b in overlay:   # 個体ブロブはKBの後(=より新しい文脈として上に積む)
            write_rollout_item("response_item", b)
        write_rollout_item("response_item", {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": CHARTER}]})
    print(f"鋳造: {sid}")
    print(f"  KB[{name}] 既読{len(st['fed'])}本 / ブロブ{len(blobs)}個"
          + (f" + 個体ブロブ{len(overlay)}個(overlay)" if overlay else "") + f" / {path}")
    if not a.no_register:
        open(os.path.join(KB_HOME, "kb-session"), "w").write(sid + "\n")
        print(f"  kb-session 更新済み — spawn系はこのidからforkする")
    print(f"\ncodex fork {sid}")

if __name__ == "__main__":
    main()
