#!/usr/bin/env python3
"""放映室：在浏览器里看成片、打带时间码和截帧的反馈，看进度表、素材库和剪辑时间线。默认做法，可改可换。
  python3 .claude/tools/studio.py [--port 8787] [--root 项目根] [--open]
  只绑 127.0.0.1；只读项目目录，路径里带点的部分（.env、.claude）一律不发；
  每个请求校验 Host（防 DNS 重绑定）；写入（POST/PUT/DELETE）还要 Origin 是本机这个端口、POST/PUT 必须是 JSON——别的网页写不进来；
  反馈写进 反馈/待处理.md，截帧存 反馈/帧/；页面在同目录 studio.html。"""
import argparse, base64, hashlib, shutil, tempfile, datetime, importlib.util, json, mimetypes, os, pathlib, re, sys, threading, urllib.parse, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import ffprobe
import common as C
import editing
import upscaling
import quality
import agent_api
import automation
import workspace
import generation
import hmac

VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
IMPORT_ROOTS = []
ROOT = pathlib.Path.cwd()
VIDEO = {".mp4", ".mov", ".webm", ".m4v"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac"}
TEXT = {".md", ".txt", ".json"}
MEDIA = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".wav": "audio/wav",
         ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".md": "text/markdown; charset=utf-8",
         ".txt": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8", ".html": "text/html; charset=utf-8"}
FB, FRAMES, EDL, SEG = C.FB, C.FB_FRAMES, C.EDL, C.SEG
COLS = ("脚本", "参考", "生成", "过审", "入片", "验收")
PORT = 8787
HEAD = ("# 待处理反馈\n\n> 放映室写入：哪个版本、第几秒、他说的话、截帧（同名 _s.jpg 是缩略图）。拷问时逐条读、问透后进需求文档；处理完的删掉那一行。\n\n"
        "| 时间 | 版本 | 时间码 | 你说的 | 帧 |\n|---|---|---|---|---|\n")
_probe, _edl, _edl_lock = {}, {}, threading.Lock()
REVIEW = "反馈/评审记录.json"
_review_lock, _hashes = threading.RLock(), {}


def find_root(start):
    p = pathlib.Path(start).resolve()
    for d in [p, *p.parents]:
        if any((d / m).exists() for m in (C.DOC, C.SCRIPT_DIR, C.LIB, C.FINAL)):
            return d
    return p


def rel(p):
    p = pathlib.Path(p)
    try:
        return str(p.absolute().relative_to(ROOT))
    except ValueError:
        return str(p.resolve().relative_to(ROOT))


def safe(relpath):
    """URL 路径 → 项目内的绝对路径；越界或带点的部分返回 None。"""
    raw = urllib.parse.unquote(relpath).lstrip("/")
    try:
        p = (ROOT / raw).resolve()
        parts = p.relative_to(ROOT).parts
    except (ValueError, OSError):
        return None
    return None if any(x.startswith(".") for x in parts) else p


def kind(p):
    s = p.suffix.lower()
    return "video" if s in VIDEO else "image" if s in IMAGE else "audio" if s in AUDIO else "text" if s in TEXT else "other"


def probe(p):
    key = (str(p), p.stat().st_mtime)
    if key not in _probe:
        info = ffprobe(p)
        fps = info.get("fps")
        if isinstance(fps, str) and "/" in fps:
            a, b = fps.split("/")
            fps = round(float(a) / float(b), 3) if float(b) else None
        info["fps"] = fps or 25
        _probe[key] = info
    return _probe[key]


def fparts(p, extra=None):
    d = {"path": rel(p), "name": p.name, "kind": kind(p), "size": p.stat().st_size}
    if extra:
        d.update(extra)
    return d


def walk(d, depth=3):
    d = ROOT / d
    if not d.is_dir():
        return
    base = len(d.parts)
    for cur, dirs, files in os.walk(d, followlinks=True):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        if len(pathlib.Path(cur).parts) - base >= depth:
            dirs[:] = []
        for f in sorted(files):
            if not f.startswith("."):
                yield pathlib.Path(cur) / f


def api_project():
    keys = (C.DOC, SEG, C.LIB, C.GEN, C.EDIT, EDL, C.FINAL, FB)
    return {"version": VERSION, "name": ROOT.name, "root": str(ROOT), "has": {k: (ROOT / k).exists() for k in keys}}


def final_videos():
    """final/ 顶层的成片和 _旧版_vN/ 里的旧版，按 (文件, 版本号或 None, 文件夹名) 给出。"""
    fin = ROOT / "final"
    if not fin.is_dir():
        return
    for p in sorted(fin.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_file() and kind(p) == "video":
            yield p, None, ""
        elif p.is_dir() and p.name.startswith("_旧版"):
            m = re.search(r"v(\d+)", p.name)
            for q in sorted(p.iterdir()):
                if q.is_file() and kind(q) == "video":
                    yield q, (int(m.group(1)) if m else 0), p.name


def finals_sig():
    return "|".join(f"{rel(p)}:{p.stat().st_size}" for p, _, _ in final_videos())


def api_finals():
    vids, docs, fin = [], [], ROOT / "final"
    if fin.is_dir():
        docs = [fparts(p) for p in sorted(fin.iterdir()) if p.is_file() and p.suffix.lower() == ".md" and not p.name.startswith(".")]
        for p, n, folder in final_videos():
            if n is None:
                vids.append(fparts(p, {"version": "当前", "order": 10 ** 6, "current": True, **probe(p)}))
            else:
                vids.append(fparts(p, {"version": f"v{n}" if re.search(r"v\d+", folder) else folder, "order": n, "current": False, **probe(p)}))
        old = [v["order"] for v in vids if not v["current"]]
        for v in vids:
            if v["current"] and old:
                v["version"] = f"v{max(old) + 1}"
        for v in vids:
            match = re.fullmatch(r"初剪_v(\d+)\.mp4", v["name"])
            if match:
                v.update(version="初剪 v" + match.group(1), order=2 * 10 ** 6 + int(match.group(1)))
        vids.sort(key=lambda v: (-v["order"], v["name"]))
    return {"videos": vids, "docs": docs}


def api_doc(q):
    p = safe(q.get("path", [""])[0])
    if not p or not p.is_file() or kind(p) != "text":
        return None
    return {"path": rel(p), "mtime": round(p.stat().st_mtime, 3), "text": p.read_text(encoding="utf-8", errors="replace")[:2_000_000]}


DISCARD = re.compile(r"废弃|discard|trash|_old", re.I)


def load_record(p):
    """候选视频旁边的 同名.json（适配写的登记）就是这条的生成记录。"""
    for q in (p.with_name(p.name + ".json"), p.with_suffix(".json")):
        if q.is_file() and q.stat().st_size < 400_000:
            try:
                return rel(q), json.loads(q.read_text(encoding="utf-8"))
            except Exception:
                return rel(q), None
    return None, None


def seg_files(sid, files):
    out = {"candidates": [], "product": [], "faces": [], "sheet": [], "stt": [], "other": []}
    pat = re.compile(r"(^|[_\-./ ])" + re.escape(sid) + r"([_\-./ ]|$)")
    hit = [p for p in files if pat.search(rel(p))]
    used = set()
    for p in hit:
        if kind(p) != "video":
            continue
        recp, rec = load_record(p)
        if recp:
            used.add(recp)
        rec = rec if isinstance(rec, dict) else None
        out["candidates"].append(fparts(p, {"selected": bool(re.search(r"选中|sel|chosen|final", p.name.lower())), "discarded": bool(DISCARD.search(rel(p))),
                                            "record": recp, "job": (rec or {}).get("job"), "status": (rec or {}).get("status"), "info": (rec or {}).get("info"), "raw": rec}))
    for p in hit:
        k, n = kind(p), p.name.lower()
        if k == "video" or rel(p) in used:
            continue
        if k == "image" and re.search(r"product|产品|对比", n):
            out["product"].append(fparts(p))
        elif k == "image" and re.search(r"face|脸", n):
            out["faces"].append(fparts(p))
        elif k == "image" and re.search(r"sheet|抽帧|contact|frames|grid", n):
            out["sheet"].append(fparts(p))
        elif k == "text" and re.search(r"stt|听写|transcript|whisper", n):
            out["stt"].append(fparts(p, {"text": p.read_text(encoding="utf-8", errors="replace")[:4000]}))
        elif k in ("image", "text", "audio"):
            out["other"].append(fparts(p))
    out["candidates"].sort(key=lambda c: (c["discarded"], c["name"]))
    return out


def api_segments():
    f = ROOT / SEG
    if not f.is_file():
        return {"found": False, "mtime": None, "gen": _newest(C.GEN)}
    text = f.read_text(encoding="utf-8")
    blocks = {}
    for b in re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S):
        try:
            j = json.loads(b)
            blocks[str(j.get("id", ""))] = j
        except json.JSONDecodeError:
            pass
    files = list(walk(C.GEN))
    rows, header = [], None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if cells and cells[0] == "段":
                header = cells
            continue
        if not cells or not cells[0] or set(cells[0]) <= set("-: ") or cells[0].startswith("示例"):
            continue
        row = dict(zip(header, cells + [""] * (len(header) - len(cells))))
        sid = row.get("段", "")
        fl = seg_files(sid, files)
        m = re.search(r"(?<!候)选\s*([\w.\-]+)", row.get("生成", ""))   # "候选 2 选 v2" 里取的是后面那个"选"
        if m and not any(c["selected"] for c in fl["candidates"]):
            tok = m.group(1)
            for c in fl["candidates"]:
                stem = pathlib.Path(c["name"]).stem
                c["selected"] = stem.endswith("_" + tok) or stem == tok or stem == f"{sid}_{tok}"
        rows.append({"id": sid, "scene": row.get("节拍") or row.get("场", ""), "text": row.get("内容", ""), "dur": row.get("秒", ""),
                     "cells": {k: row.get(k, "") for k in COLS},
                     "json": blocks.get(sid), "files": fl})
    state = review_read()
    for row in rows:
        entry = state["segments"].get(row["id"], {})
        candidates = {c["path"]: c for c in row["files"]["candidates"]}
        for path in entry.get("paths", []):
            p = candidate_file(path, required=False)
            if path not in candidates:
                candidates[path] = fparts(p) if p else {"path": path, "name": pathlib.Path(path).name, "missing": True}
        selected = entry.get("selected")
        for path, c in candidates.items():
            p = candidate_file(path, required=False)
            c["fingerprint"] = file_hash(p) if p else None
            record = entry.get("reviews", {}).get(path)
            c["review"] = review_status(record, c["fingerprint"])
            c["review_record"] = record
            c["selected"] = bool(selected and selected["path"] == path)
            c["selection_stale"] = bool(c["selected"] and selected["fingerprint"] != c["fingerprint"])
        row["files"]["candidates"] = list(candidates.values())
        chosen = candidates.get((selected or {}).get("path"))
        row["cells"]["生成"] = f"候选 {len(candidates)}" + ("，已选用" if selected else "")
        row["cells"]["过审"] = ("需复核" if chosen and chosen["selection_stale"] else chosen["review"]) if chosen else "待审"
        row["cells"]["验收"] = "按版本验收"
        row["review_entry"] = entry
    counts = {c: sum(1 for r in rows if r["cells"][c] and not re.search(r"待|✗|退回|复核", r["cells"][c])) for c in COLS}
    counts["过审"] = sum(r["cells"]["过审"] == "通过" for r in rows)
    cand = sum(len(r["files"]["candidates"]) for r in rows)
    status = (f"{len(rows)} 段 · 脚本 {counts['脚本']} · 参考 {counts['参考']} · 生成 {counts['生成']}（候选 {cand}）"
              f"· 过审 {counts['过审']} · 入片 {counts['入片']} · 验收请选成片版本")
    return {"found": True, "path": rel(f), "rows": rows, "status": status, "mtime": _mt(SEG), "gen": _newest(C.GEN),
            "review_mtime": _mt(REVIEW), "library": _newest(C.LIB)}


def file_hash(p):
    if not p or not p.is_file():
        return None
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if key not in _hashes:
        with p.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        _hashes[key] = digest.hexdigest()
    return _hashes[key]


def review_read():
    p = safe(REVIEW)
    if not p:
        raise PermissionError("评审记录路径越界")
    if not p.exists():
        return {"schema": 1, "segments": {}, "history": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("segments"), dict) or not isinstance(data.get("history"), list):
        raise ValueError("评审记录格式不正确；已保留原文件")
    return data


def review_write(data):
    p = safe(REVIEW)
    if not p:
        raise PermissionError("评审记录路径越界")
    p.parent.mkdir(parents=True, exist_ok=True)
    # 只保留一份写入前备份；临时文件与目标在同一目录，替换是原子的。
    if p.exists():
        backup = safe(REVIEW + ".bak")
        if not backup:
            raise PermissionError("备份路径越界")
        shutil.copyfile(p, backup)
    fd, name = tempfile.mkstemp(prefix="review-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, p)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def candidate_file(path, required=True):
    p = safe(path) if isinstance(path, str) else None
    if p and p.is_file() and kind(p) == "video" and any(p.is_relative_to(ROOT / d) for d in (C.GEN, C.LIB)):
        return p
    if required:
        raise ValueError("候选必须是生成/或素材库/内的视频")
    return None


def review_status(record, fingerprint):
    if not record:
        return "待审"
    if not fingerprint or record["fingerprint"] != fingerprint:
        return "需复核"
    return {"approved": "通过", "rejected": "退回", "pending": "待审"}[record["status"]]


def api_candidates():
    paths = {rel(p): p for d in (C.GEN, C.LIB) for p in walk(d) if candidate_file(rel(p), required=False)}
    return {"items": [fparts(p) for _, p in sorted(paths.items())]}


def segment_row(sid):
    row = next((r for r in api_segments().get("rows", []) if r["id"] == sid), None)
    if not row:
        raise ValueError("分段表中没有这个镜头")
    return row


def review_context(sid, final_path):
    row = segment_row(sid)
    finals = {rel(p): (p, n) for p, n, _ in final_videos()}
    if final_path not in finals:
        raise ValueError("请选择实际存在的成片")
    film, _ = finals[final_path]
    exported = editing.manifest(sys.modules[__name__], final_path)
    timeline = exported["timeline"] if exported else api_timeline()
    clips = timeline.get("V", [])
    entry = row["review_entry"]
    # 只接受明确的段号/节拍名称或已选素材匹配，不靠列表下标猜镜头。
    matches = [c for c in clips if str(c["id"]) in {sid, row["scene"]}]
    if not matches:
        matches = [c for c in clips if c["src"] == (entry.get("selected") or {}).get("path")]
    clip = matches[0] if len(matches) == 1 else None
    binding = entry.get("bindings", {}).get(final_path)
    confirmed = False
    # 每个成片都要显式确认剪辑表对应关系；旧版也不能借用当前表直接批准。
    result = {"segment": sid, "final": final_path, "clip": clip, "confirmed": confirmed,
              "film_hash": file_hash(film), "status": "待审", "neighbors": [],
              "reason": "" if clip else "剪辑表不能唯一匹配这一段，请核对镜头ID或节拍名称"}
    if clip:
        idx = clip["i"]
        window = clips[max(0, idx - 1):idx + 2]
        result["neighbors"] = window
        start, end = window[0]["start"], window[-1]["start"] + window[-1]["dur"]
        sources = {c["src"]: (exported["sources"].get(c["src"]) if exported else file_hash(safe(c["src"]))) for c in window}
        audio = [x for x in timeline.get("A", []) if x["start"] < end and x["start"] + x["dur"] > start]
        sources.update({x["src"]: file_hash(safe(x["src"])) for x in audio})
        snapshot = {"film": result["film_hash"], "clips": window, "sources": sources, "audio": audio,
                    "subs": [x for x in timeline.get("subs", []) if x["start"] < end and x["end"] > start],
                    "look": timeline.get("look"), "target_i": timeline.get("target_i"), "duck_db": timeline.get("duck_db"),
                    "selected": ({"path": clip["src"], "fingerprint": exported["sources"].get(clip["src"])} if exported else entry.get("selected"))}
        result["signature"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        confirmed = bool(binding and binding == {"film": result["film_hash"], "signature": result["signature"]})
        confirmed = confirmed or bool(exported)
        result["confirmed"] = confirmed
        records = entry.get("acceptances", [])
        record = next((x for x in reversed(records) if x["final"] == final_path), None)
        result["record"] = record
        result["status"] = review_status({**record, "fingerprint": record["signature"]} if record else None, result["signature"])
        if record and not confirmed:
            result["status"] = "需复核"
    return result


def review_mutate(body):
    if not isinstance(body, dict):
        raise ValueError("请求必须是对象")
    sid, action = body.get("segment"), body.get("action")
    with _review_lock:
        row = segment_row(sid)
        state = review_read()
        entry = state["segments"].setdefault(sid, {})
        event = {"segment": sid, "action": action, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if action in {"associate", "unlink", "select", "review"}:
            path = body.get("path")
            p = candidate_file(path, required=action != "unlink")
            path = rel(p) if p else path
            linked = [c["path"] for c in row["files"]["candidates"]]
            fingerprint = file_hash(p)
            if action != "associate" and path not in linked:
                raise ValueError("请先关联候选")
            if action in {"select", "review"} and body.get("fingerprint") != fingerprint:
                raise ValueError("素材已变化，请刷新后再评审")
            paths = entry.setdefault("paths", [])
            if action == "associate":
                if path not in paths:
                    paths.append(path)
            elif action == "unlink":
                # 自动发现的生成目录候选仍会显示，不隐藏来源文件。
                if path not in paths:
                    raise ValueError("自动发现的候选不能解除关联；可将它退回")
                paths.remove(path)
                if (entry.get("selected") or {}).get("path") == path:
                    entry.pop("selected", None)
            elif action == "select":
                entry["selected"] = {"path": path, "fingerprint": fingerprint}
            else:
                status, note = body.get("status"), str(body.get("note", "")).strip()[:4000]
                if status not in {"approved", "rejected", "pending"}:
                    raise ValueError("未知评审状态")
                if status == "rejected" and not note:
                    raise ValueError("请填写退回原因")
                entry.setdefault("reviews", {})[path] = {"fingerprint": fingerprint, "status": status, "note": note, "at": event["at"]}
                event.update(status=status, note=note)
            event.update(path=path, fingerprint=fingerprint)
        elif action in {"bind", "accept"}:
            ctx = review_context(sid, body.get("final"))
            if not ctx["clip"]:
                raise ValueError(ctx["reason"])
            if body.get("signature") != ctx["signature"]:
                raise ValueError("成片或剪辑已变化，请刷新后重新核对")
            if action == "bind":
                entry.setdefault("bindings", {})[ctx["final"]] = {"film": ctx["film_hash"], "signature": ctx["signature"]}
            else:
                if not ctx["confirmed"]:
                    raise ValueError("请先确认这份剪辑表对应所选成片")
                status, note = body.get("status"), str(body.get("note", "")).strip()[:4000]
                if status not in {"approved", "rejected", "pending"}:
                    raise ValueError("未知验收状态")
                if status == "rejected" and not note:
                    raise ValueError("请填写退回原因")
                # 成片验收不强制候选已通过：旧项目的选片记录可能尚未迁移，二者仍独立存储。
                record = {"final": ctx["final"], "signature": ctx["signature"], "status": status, "note": note, "at": event["at"], "clip": ctx["clip"]}
                entry.setdefault("acceptances", []).append(record)
                event.update(status=status, note=note)
            event.update(final=ctx["final"], signature=ctx["signature"])
        else:
            raise ValueError("未知操作")
        state["history"].append(event)
        review_write(state)
    return {"ok": True}



def api_library():
    """素材库/ 下一层文件夹是类别（产品、模特、场景、关键帧、道具，或别的名字）。同类里所有文件都共用的第一个词不算名字（set_、ref_ 这种），
    之后的第一个词是名字，再往后是变体；放在更深一层文件夹里的，文件夹名就是名字。旁边的 同名.prompt.txt 当作这张图的出图词。"""
    if not (ROOT / C.LIB).is_dir():
        return {"found": False, "mtime": None}
    raw = []
    for p in walk(C.LIB, depth=3):
        if kind(p) not in ("image", "audio", "video"):
            continue
        parts = pathlib.Path(rel(p)).parts
        cat = parts[1] if len(parts) > 2 else "其他"
        sub = parts[2] if len(parts) > 3 else None
        prompt = ""
        pt = p.with_name(p.name + ".prompt.txt")
        if pt.is_file():
            prompt = pt.read_text(encoding="utf-8", errors="replace")[:2000].strip()
        raw.append((p, cat, sub, re.split(r"[_\-]+", p.stem), prompt))
    items = []
    by_cat = {}
    for r in raw:
        by_cat.setdefault(r[1], []).append(r)
    for cat, rows in by_cat.items():
        firsts = {r[3][0] for r in rows if not r[2]}
        strip = len(firsts) == 1 and sum(1 for r in rows if not r[2]) > 1  # 整类都以同一个词开头：那是类型词，不是名字
        for p, cat, sub, toks, prompt in rows:
            if sub:
                name, variant = sub, p.stem
            else:
                t = toks[1:] if strip and len(toks) > 1 else toks
                name, variant = t[0], "_".join(t[1:])
            items.append(fparts(p, {"cat": cat, "name": name, "variant": variant, "prompt": prompt}))
    return {"found": True, "items": items, "mtime": _newest(C.LIB)}


def kind_of(path):
    s = path.lower()
    if any(k in s for k in ("music", "score", "配乐")):
        return "music"
    if any(k in s for k in ("bed", "amb", "环境", "roomtone")):
        return "amb"
    if any(k in s for k in ("voice", "/vo", "对白", "配音")):
        return "voice"
    return "sfx"


def load_edl():
    """剪辑表是一个 python 文件（总装用的那份），这里按它自己的目录当 __file__、项目根当工作目录执行一次，按修改时间缓存。"""
    f = ROOT / EDL
    if not f.is_file():
        return None
    st = f.stat()
    key = (str(f.resolve()), st.st_mtime_ns, st.st_size)
    with _edl_lock:
        if _edl.get("key") == key:
            return _edl["mod"]
        old = os.getcwd()
        os.chdir(ROOT)
        try:
            spec = importlib.util.spec_from_file_location("edl_view", str(f))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        finally:
            os.chdir(old)
        _edl.update(key=key, mod=m)
        return m


def api_timeline():
    m = load_edl()
    if m is None:
        return {"found": False, "path": EDL, "mtime": None}
    V, A, SUBS = list(getattr(m, "V", [])), list(getattr(m, "A", [])), list(getattr(m, "SUBS", []))
    starts, t, vitems = [], 0.0, []
    for i, it in enumerate(V):
        src = str(it.get("src", ""))
        d = float(it.get("out", 0)) - float(it.get("in", 0)) + float(it.get("freeze", 0) or 0)
        k = ("black" if src == "BLACK" else "white" if src == "WHITE" else "title" if src.startswith(("TITLE:", "CARD:"))
             else "image" if src.lower().endswith((".png", ".jpg", ".jpeg")) else "live")
        p = (ROOT / src) if k in ("live", "image") else None
        starts.append(t)
        vitems.append({"i": i, "id": it.get("id") or (("品牌卡" if src.startswith("CARD:") else src.split(":", 1)[1].split("|")[0]) if k == "title" else os.path.basename(src)), "src": src, "kind": k,
                       "start": round(t, 3), "dur": round(d, 3), "in": it.get("in"), "out": it.get("out"), "grade": it.get("grade"),
                       "matte": it.get("matte", True), "a": it.get("a", 1.0), "fi": it.get("fi"), "fo": it.get("fo"), "freeze": it.get("freeze"),
                       "exists": bool(p and p.is_file())})
        t += d
    total = t

    def at_of(x):
        if isinstance(x, (list, tuple)):
            return (starts[int(x[1])] if int(x[1]) < len(starts) else 0.0) + float(x[2])
        return float(x)
    aitems = []
    for j, ev in enumerate(A):
        src = str(ev.get("src", ""))
        p = ROOT / src
        ex = p.is_file()
        s = at_of(ev.get("at", 0))
        k = ev.get("kind") or kind_of(src)
        d = ev.get("dur")
        if d is None:
            d = max(0.5, (probe(p).get("duration", 0) or 0) - float(ev.get("in", 0) or 0)) if ex else 3.0
        d = min(float(d), max(0.0, total - s)) if total else float(d)
        aitems.append({"i": j, "src": src, "name": os.path.basename(src), "kind": k, "start": round(s, 3), "dur": round(d, 3),
                       "gain": ev.get("gain", 0), "loop": bool(ev.get("loop")), "fi": ev.get("fi"), "fo": ev.get("fo"), "duck": bool(ev.get("duck")),
                       "norm": ev.get("norm"), "in": ev.get("in", 0), "fx": ev.get("fx"), "raw": bool(ev.get("raw")), "clarity": bool(ev.get("clarity")),
                       "hits": [round(s + float(x[0]), 3) for x in (ev.get("hits") or [])],
                       "env": [[round(s + float(x[0]), 3), x[1]] for x in (ev.get("env") or [])], "exists": ex})
    subs = []
    for sb in SUBS:
        try:
            if sb[0] == "V":
                subs.append({"start": round(starts[sb[1]] + sb[2], 3), "end": round(starts[sb[1]] + sb[3], 3), "text": sb[4]})
            else:
                subs.append({"start": float(sb[0]), "end": float(sb[1]), "text": sb[2]})
        except Exception:
            pass
    sync = None
    sf = ROOT / C.EDIT / "sync.json"
    if sf.is_file():
        try:
            sync = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            sync = None
    return {"found": True, "path": EDL, "mtime": _mt(EDL), "look": getattr(m, "LOOK", "none"), "target_i": getattr(m, "TARGET_I", None),
            "duck_db": getattr(m, "DUCK_DB", None), "total": round(total, 3), "sync": sync, "V": vitems, "A": aitems, "subs": subs}


def fb_lines():
    f = ROOT / FB
    return f.read_text(encoding="utf-8").splitlines() if f.is_file() else []


def fb_is_row(l):
    return l.startswith("|") and not l.startswith("| 时间") and not l.startswith("|-")


def fb_cells(l):
    c = [x.strip() for x in l.strip().strip("|").split("|")]
    return (c + [""] * 5)[:5]


def api_feedback_get():
    rows = []
    for i, l in enumerate(fb_lines()):
        if not fb_is_row(l):
            continue
        when, ver, tc, note, frame = fb_cells(l)
        thumb = ""
        if frame:
            tp = ROOT / (frame.rsplit(".", 1)[0] + "_s.jpg")
            thumb = rel(tp) if tp.is_file() else ""
        rows.append({"line": i, "when": when, "version": ver, "tc": tc, "note": note, "frame": frame, "thumb": thumb})
    return {"rows": rows, "count": len(rows), "path": FB, "mtime": _mt(FB)}


def fb_write(lines):
    (ROOT / FB).write_text("\n".join(lines) + "\n", encoding="utf-8")


def fb_update(body):
    lines = fb_lines()
    i = int(body.get("line", -1))
    if not (0 <= i < len(lines)) or not fb_is_row(lines[i]):
        raise ValueError("没有这一行")
    c = fb_cells(lines[i])
    if body.get("tc") and (c[1], c[2]) != (str(body.get("version", "")), str(body.get("tc", ""))):
        raise ValueError("反馈文件已经被改过，刷新一下再改")
    c[3] = str(body.get("note", "")).replace("|", "｜").replace("\n", " ").strip()
    lines[i] = "| " + " | ".join(c) + " |"
    fb_write(lines)
    return api_feedback_get()


def fb_delete(q):
    lines = fb_lines()
    i = int(q.get("line", ["-1"])[0])
    if not (0 <= i < len(lines)) or not fb_is_row(lines[i]):
        raise ValueError("没有这一行")
    c = fb_cells(lines[i])
    want = (q.get("version", [""])[0], q.get("tc", [""])[0])
    if want[1] and (c[1], c[2]) != want:
        raise ValueError("反馈文件已经被改过，刷新一下再删")
    frame = c[4]
    del lines[i]
    fb_write(lines)
    if frame:
        for p in (ROOT / frame, ROOT / (frame.rsplit(".", 1)[0] + "_s.jpg")):
            if p.is_file() and rel(p).startswith(FRAMES):
                p.unlink()
    return api_feedback_get()


def _mt(p):
    try:
        return round((ROOT / p).stat().st_mtime, 3)
    except OSError:
        return None


def _newest(d):
    try:
        return round(max([(ROOT / d).stat().st_mtime] + [p.stat().st_mtime for p in walk(d)]), 3)
    except (OSError, ValueError):
        return None


def api_stat():
    """页面每几秒问一次：项目里哪些东西变了。只报修改时间，不读内容。"""
    fin = ROOT / "final"
    docs = {rel(p): round(p.stat().st_mtime, 3) for p in fin.glob("*.md")} if fin.is_dir() else {}
    return {"finals": finals_sig(), "docs": docs, "edl": _mt(EDL), "segments": _mt(SEG), "gen": _newest(C.GEN),
            "feedback": _mt(FB), "library": _newest(C.LIB), "review": _mt(REVIEW)}


def save_frame(body):
    m = re.match(r"data:image/(png|jpeg);base64,(.+)", body.get("data", ""), re.S)
    if not m:
        raise ValueError("没有图")
    (ROOT / FRAMES).mkdir(parents=True, exist_ok=True)
    ext = "png" if m.group(1) == "png" else "jpg"
    tc = re.sub(r"[^0-9]+", "-", str(body.get("tc", "0"))).strip("-")
    ver = re.sub(r"[^\w]+", "", str(body.get("version", "v"))) or "v"
    p, i = ROOT / FRAMES / f"{ver}_{tc}.{ext}", 2
    while p.exists():
        p, i = ROOT / FRAMES / f"{ver}_{tc}_{i}.{ext}", i + 1
    p.write_bytes(base64.b64decode(m.group(2)))
    out = {"path": rel(p), "thumb": ""}
    mt = re.match(r"data:image/jpeg;base64,(.+)", body.get("thumb") or "", re.S)
    if mt:
        tp = p.with_name(p.stem + "_s.jpg")
        tp.write_bytes(base64.b64decode(mt.group(1)))
        out["thumb"] = rel(tp)
    return out


def delete_frame(q):
    p = safe(q.get("path", [""])[0])
    if p and p.is_file() and rel(p).startswith(FRAMES):
        p.unlink()
        return {"ok": True}
    return {"ok": False}


def append_feedback(body):
    items = body.get("items", [])
    f = ROOT / FB
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_text(HEAD, encoding="utf-8")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(f, "a", encoding="utf-8") as fh:
        for it in items:
            note = str(it.get("note", "")).replace("|", "｜").replace("\n", " ").strip()
            fh.write(f"| {now} | {it.get('version', '')} | {it.get('tc', '')} | {note} | {it.get('frame') or ''} |\n")
    out = api_feedback_get()
    out["added"] = len(items)
    return out


class H(BaseHTTPRequestHandler):
    server_version = "LazyStudio/" + VERSION

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _text(self, s, status=404):
        b = s.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise PermissionError("只收 JSON")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 40_000_000:
            raise PermissionError("太大了")
        return json.loads(self.rfile.read(n) or b"{}")

    def _host_ok(self):
        """Host 必须是本机这个端口：挡住 DNS 重绑定（别的域名解析到 127.0.0.1 来读写）。"""
        return (self.headers.get("Host") or "").lower() in {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

    def _origin_ok(self):
        """写入必须来自本机这个页面：浏览器跨站发的请求都带别人的 Origin，直接拒。"""
        return (self.headers.get("Origin") or "").lower() in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"http://[::1]:{PORT}"}

    def _guard(self, write=False):
        if not self._host_ok():
            self._text("只接受本机地址（Host 不对）", 403)
            return False
        if write and not self._origin_ok():
            self._text("只接受放映室页面自己发的写入（Origin 不对）", 403)
            return False
        return True

    def _file(self, p):
        size = p.stat().st_size
        ctype = MEDIA.get(p.suffix.lower()) or mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and size:
            a, b = m.groups()
            if a:
                start, end = int(a), (int(b) if b else size - 1)
            elif b:
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlsplit(self.path)
        q, path = urllib.parse.parse_qs(u.query), u.path
        try:
            if path in ("/", "/index.html", "/studio.html"):
                return self._file(HERE / "studio.html")
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/issue-location":
                return self._json(editing.issue_location(sys.modules[__name__], q.get("id", [""])[0], q.get("final", [""])[0]))
            if path == "/api/upscale-compare":
                return self._json(upscaling.comparison(sys.modules[__name__], q.get("id", [""])[0]))
            if path == "/api/agent/project":
                if not self._agent_guard(): return
                return self._json(agent_api.project(sys.modules[__name__]))
            if path == "/api/tasks":
                return self._json(agent_api.dispatch(sys.modules[__name__], {"operation":"tasks.list","arguments":{"limit":200}})["result"])
            if path == "/api/generation":
                return self._json({"items":generation.jobs(sys.modules[__name__]),"providers":generation.capabilities()})
            if path == "/api/workspace":
                return self._json(workspace.overview(sys.modules[__name__],q.get("final",[None])[0],q.get("source",["current"])[0]))
            if path == "/api/automation":
                return self._json({"items":automation.jobs(sys.modules[__name__])})
            if path == "/api/quality":
                return self._json(quality.read(sys.modules[__name__]))
            if path == "/api/quality-context":
                return self._json(quality.context(sys.modules[__name__], q.get("segment", [""])[0], q.get("final", [""])[0]))
            if path == "/api/quality-reports":
                return self._json(quality.reports(sys.modules[__name__]))
            if path == "/api/upscale":
                return self._json(upscaling.status(sys.modules[__name__]))
            if path == "/api/issues":
                return self._json(editing.issues(sys.modules[__name__]))
            if path == "/api/edit":
                return self._json(editing.read(sys.modules[__name__]))
            if path == "/api/exports":
                return self._json({"jobs": editing.jobs(sys.modules[__name__])})
            if path == "/api/project":
                return self._json(api_project())
            if path == "/api/finals":
                return self._json(api_finals())
            if path == "/api/segments":
                return self._json(api_segments())
            if path == "/api/candidates":
                return self._json(api_candidates())
            if path == "/api/review-context":
                return self._json(review_context(q.get("segment", [""])[0], q.get("final", [""])[0]))
            if path == "/api/library":
                return self._json(api_library())
            if path == "/api/feedback":
                return self._json(api_feedback_get())
            if path == "/api/timeline":
                final = q.get("final", [""])[0]
                exported = editing.manifest(sys.modules[__name__], final) if final else None
                return self._json({**exported["timeline"], "exported": True, "mtime": _mt(EDL)} if exported else api_timeline())
            if path == "/api/stat":
                return self._json(api_stat())
            if path == "/api/doc":
                d = api_doc(q)
                return self._json(d) if d else self._text("没有这个文件", 404)
            if path.startswith("/f/"):
                p = safe(path[3:])
                return self._file(p) if p and p.is_file() else self._text("没有这个文件", 404)
            self._text("没有这个地址", 404)
        except ValueError as e:
            self._text(str(e), 400)
        except PermissionError as e:
            self._text(f"拒绝：{e}", 403)
        except Exception as e:
            self._text(f"出错：{e}", 500)

    def _agent_guard(self):
        if not self._host_ok():
            self._text("只允许本机Host",403)
            return False
        if self.headers.get('Origin'):
            self._text("智能体入口不接受浏览器Origin",403)
            return False
        value=self.headers.get('Authorization','')
        if not hmac.compare_digest(value,'Bearer '+agent_api.token(sys.modules[__name__])):
            self._text("智能体令牌无效",403)
            return False
        return True

    def do_POST(self):
        with agent_api.LOCK:
            return self._dispatch_post()

    def _dispatch_post(self):
        if urllib.parse.urlsplit(self.path).path == '/api/agent/call':
            if not self._agent_guard(): return
            try: return self._json(agent_api.dispatch(sys.modules[__name__],self._body()))
            except ValueError as e: return self._text(str(e),400)
            except PermissionError as e: return self._text(str(e),403)
            except Exception as e: return self._text(str(e),500)
        if not self._guard(write=True):
            return
        u = urllib.parse.urlsplit(self.path)
        try:
            if u.path == "/api/generation":
                return self._json(generation.submit(sys.modules[__name__],self._body()))
            if u.path == "/api/generation/retry":
                return self._json(generation.retry(sys.modules[__name__],self._body()))
            if u.path == "/api/generation/export":
                return self._json(generation.export(sys.modules[__name__],self._body()))
            if u.path == "/api/generation/apply":
                return self._json(generation.apply(sys.modules[__name__],self._body()))
            if u.path == "/api/automation/apply":
                return self._json(automation.apply_suggestion(sys.modules[__name__],self._body()))
            if u.path == "/api/automation":
                return self._json(automation.start(sys.modules[__name__],self._body()))
            if u.path == "/api/automation/reopen":
                return self._json(automation.reopen(sys.modules[__name__],self._body()))
            if u.path == "/api/automation/resume":
                return self._json(automation.resume(sys.modules[__name__],self._body().get("run_id")))
            if u.path == "/api/automation/cancel":
                return self._json(automation.cancel(sys.modules[__name__],self._body().get("run_id")))
            if u.path == "/api/quality":
                return self._json(quality.mutate(sys.modules[__name__], self._body()))
            if u.path == "/api/quality-scan":
                return self._json(quality.start(sys.modules[__name__], self._body()))
            if u.path == "/api/upscale":
                return self._json(upscaling.submit(sys.modules[__name__], self._body()))
            if u.path == "/api/upscale/cancel":
                return self._json(upscaling.cancel(sys.modules[__name__], self._body()))
            if u.path == "/api/issues":
                return self._json(editing.issues(sys.modules[__name__], self._body()))
            if u.path == "/api/edit":
                return self._json(editing.mutate(sys.modules[__name__], self._body()))
            if u.path == "/api/export/cancel":
                return self._json(editing.cancel(sys.modules[__name__], self._body()))
            if u.path == "/api/export":
                return self._json(editing.start(sys.modules[__name__], self._body()))
            if u.path == "/api/review":
                return self._json(review_mutate(self._body()))
            if u.path == "/api/frame":
                return self._json(save_frame(self._body()))
            if u.path == "/api/feedback":
                return self._json(append_feedback(self._body()))
            self._text("没有这个地址", 404)
        except ValueError as e:
            self._text(str(e), 400)
        except PermissionError as e:
            self._text(f"拒绝：{e}", 403)
        except Exception as e:
            self._text(f"出错：{e}", 500)

    def do_PUT(self):
        with agent_api.LOCK:
            return self._dispatch_put()

    def _dispatch_put(self):
        if not self._guard(write=True):
            return
        u = urllib.parse.urlsplit(self.path)
        try:
            if u.path == "/api/feedback":
                return self._json(fb_update(self._body()))
            self._text("没有这个地址", 404)
        except ValueError as e:
            self._text(str(e), 400)
        except PermissionError as e:
            self._text(f"拒绝：{e}", 403)
        except Exception as e:
            self._text(f"出错：{e}", 500)

    def do_DELETE(self):
        with agent_api.LOCK:
            return self._dispatch_delete()

    def _dispatch_delete(self):
        if not self._guard(write=True):
            return
        u = urllib.parse.urlsplit(self.path)
        try:
            if u.path == "/api/frame":
                return self._json(delete_frame(urllib.parse.parse_qs(u.query)))
            if u.path == "/api/feedback":
                return self._json(fb_delete(urllib.parse.parse_qs(u.query)))
            self._text("没有这个地址", 404)
        except ValueError as e:
            self._text(str(e), 400)
        except PermissionError as e:
            self._text(f"拒绝：{e}", 403)
        except Exception as e:
            self._text(f"出错：{e}", 500)


def main():
    global ROOT, PORT, IMPORT_ROOTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--root", help="项目根目录，默认从当前目录往上找")
    ap.add_argument("--import-root", action="append", default=[], help="允许智能体导入媒体的目录，可重复指定")
    ap.add_argument("--open", action="store_true", help="启动后在浏览器里打开")
    a = ap.parse_args()
    ROOT = pathlib.Path(a.root).resolve() if a.root else find_root(os.getcwd())
    PORT = a.port
    IMPORT_ROOTS = [pathlib.Path(p).resolve() for p in a.import_root]
    try:
        service_lock = agent_api.acquire_service_lock(sys.modules[__name__])
        agent_api.token(sys.modules[__name__])
    except ValueError as e:
        sys.exit(str(e))
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        sys.exit(f"端口 {a.port} 用不了（{e}），换一个：--port 8788")
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{a.port}"
    print(f"放映室：{url}  项目：{ROOT}", flush=True)
    if a.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
