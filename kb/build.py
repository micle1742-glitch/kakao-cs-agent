"""kb 빌드: 증분 추출 → 근거 확인 → 구두 카드 → 충돌 → 회귀 테스트 → 공개본 교체.

폴더 (기본 knowledge/kb/, gitignore)
  work/state.json, work/cards.jsonl, work/rejected.jsonl   추출 캐시. 이미 처리한 단위는 다시 LLM에 보내지 않는다
  staging/                                                이번 빌드 결과 (회귀 테스트 대상)
  current/                                                봇이 읽는 공개본. 회귀 테스트를 통과해야 바뀐다
  overrides.yaml   사람이 고치는 문서 분류 {문서 id: rule|record|promo|old|unknown|skip}
  decisions.yaml   사람이 적는 충돌 판정 (conflicts.md를 보고)
  conflicts.md, classification.md, report.md               사람이 읽는 검토 파일

실패한 단위(LLM 오류 등)는 빼고 진행하고, 다음 빌드에서 다시 시도한다.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from kb import classify as classify_mod
from kb import conflicts as conflict_mod
from kb import contract, oral
from app import rules
from kb.cards import card_id, clean_values, topic_key, verify
from kb.render import render

ROOT = Path(__file__).resolve().parent.parent
ORAL_BATCH = 40
MAX_PROMPT_CHARS = int(os.getenv("KB_MAX_CHARS") or "200000")


def _read_json(path, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class Paths:
    def __init__(self, out):
        self.out = Path(out)
        self.state = self.out / "work" / "state.json"
        self.cards = self.out / "work" / "cards.jsonl"
        self.rejected = self.out / "work" / "rejected.jsonl"
        self.staging = self.out / "staging"
        self.current = self.out / "current"
        self.overrides = self.out / "overrides.yaml"
        self.decisions = self.out / "decisions.yaml"


# --- 추출 단위 만들기 ---

def document_units(kb, classes, state):
    """처리할 문서 단위. 이미 끝난 문서·건너뛸 문서·글이 없는 문서는 뺀다."""
    units = []
    for doc_id, doc in kb.documents.items():
        c = classes[doc_id]
        if c["kind"] == "skip" or c.get("sameAs") or state["units"].get(doc_id, {}).get("status") == "done":
            continue  # 건너뛸 문서, 다른 문서와 내용이 같은 문서, 이미 끝난 문서
        segments = [(ch["id"], ch["text"]) for ch in kb.by_doc.get(doc_id, []) if ch["text"].strip()]
        if sum(len(t) for _, t in segments) < 10:
            continue
        units.append({"key": doc_id, "origin": "doc", "title": doc.get("title") or doc.get("fileName"),
                      "label": c["label"], "effectiveDate": doc.get("effectiveDate"), "code": doc.get("code"),
                      "segments": segments})
    return units


def oral_units(kb, state):
    done = set(state.get("oralDone", ()))
    units = []
    pending = [f for f in kb.findings if f"finding:{f['id']}" not in done]
    for i in range(0, len(pending), ORAL_BATCH):
        segs = oral.finding_segments(pending[i:i + ORAL_BATCH])
        units.append({"key": f"findings:{segs[0][0]}", "origin": "oral", "title": "대화로 알아낸 것",
                      "label": "구두", "segments": segs, "bots": {f"finding:{f['id']}": f.get("bots", [])
                                                               for f in pending[i:i + ORAL_BATCH]}})
    for bot, segs in oral.utterance_units(kb.utterances):
        segs = [s for s in segs if s[0] not in done]
        for i in range(0, len(segs), ORAL_BATCH):
            part = segs[i:i + ORAL_BATCH]
            units.append({"key": f"utter:{bot}:{part[0][0]}", "origin": "oral", "title": f"{bot}의 말",
                          "label": "구두", "segments": part, "bots": {s[0]: [bot] for s in part}})
    return units


async def run_units(units, extractor, state, raw, rejected, known_topics, log):
    """단위마다 추출 → 근거 확인. 실패한 단위는 기록만 하고 다음으로."""
    stats = {"units": 0, "failed": 0, "accepted": 0, "rejected": 0}
    for unit in units:
        unit["known_topics"] = sorted(known_topics)
        try:
            cards = await extractor.extract(unit)
        except Exception as e:  # LLM 한도·형식 오류 등: 이 단위만 빼고 계속
            state["units"][unit["key"]] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300],
                                           "at": datetime.now().isoformat(timespec="seconds")}
            stats["failed"] += 1
            log(f"  ✗ {unit['title']}: {type(e).__name__}")
            continue
        stats["units"] += 1
        by_id = dict(unit["segments"])
        n_ok = 0
        for c in cards:
            first = [(c.get("chunkId"), by_id[c["chunkId"]])] if c.get("chunkId") in by_id else []
            anchor = verify(c, first + [s for s in unit["segments"] if s[0] != c.get("chunkId")])
            if not anchor or not str(c.get("topic", "")).strip() or not str(c.get("rule", "")).strip():
                rejected.append({"unit": unit["key"], "card": c,
                                 "why": "근거 문장이 원문에 없음" if not anchor else "주제·규칙 비어 있음"})
                stats["rejected"] += 1
                continue
            card = {"id": card_id(anchor, c["evidence"]), "unit": unit["key"], "origin": unit["origin"],
                    "anchor": anchor, "topic": c["topic"].strip(), "rule": c["rule"].strip(),
                    "values": clean_values(c.get("values")), "basis": (c.get("basis") or "").strip() or None,
                    "evidence": c["evidence"].strip()}
            if unit["origin"] == "oral":
                card["bots"] = unit["bots"].get(anchor, [])
            raw[card["id"]] = card
            known_topics.add(card["topic"])
            n_ok += 1
        stats["accepted"] += n_ok
        if unit["origin"] == "doc":
            state["units"][unit["key"]] = {"status": "done", "cards": n_ok,
                                           "at": datetime.now().isoformat(timespec="seconds")}
        else:
            state.setdefault("oralDone", []).extend(s[0] for s in unit["segments"])
        log(f"  ✓ {unit['title']}: 카드 {n_ok}장")
    return stats


# --- 조립 ---

def assemble(kb, classes, raw, decisions=None):
    """추출 캐시 + 지금의 분류(사람이 고친 것 포함) → 봇에 넣을 카드.

    카드마다 권위(id·순위·이름), 판(시행일), 기준 시점을 채운다. 기준 시점은 카드에 없으면 rules.yaml의
    쟁점별 기본값. 문서에서 못 찾은 값은 사람이 decisions.yaml의 cards: 아래에 카드 id별로 채운다
      cards:
        K1a2b3c4d5: {effectiveDate: 2037-06-20, basis: 신고일, authority: rule}
    """
    fixes = (decisions or {}).get("cards") or {}
    topic_basis = {topic_key(t): b for t, b in rules.get()["topics"].items()}
    cards = []
    for r in raw.values():
        card = dict(r)
        if r["origin"] == "doc":
            doc = kb.documents.get(r["unit"])
            c = classes.get(r["unit"])
            if not doc or not c or c["kind"] == "skip" or c.get("sameAs"):
                continue  # 이번 kb에 없는 문서, 건너뛰기로 한 문서, 같은 내용의 다른 문서로 대신한 문서
            chunk = kb.chunks.get(r["anchor"], {})
            card.update(kind=c["kind"], authority=c["rank"], authorityLabel=c["label"],
                        effectiveDate=doc.get("effectiveDate"), code=doc.get("code"), topics=chunk.get("topics", []),
                        source={"documentId": doc["id"], "fileName": doc.get("fileName"),
                                "location": chunk.get("location"), "chunkId": r["anchor"]})
        else:
            rank, label = classify_mod.authority("oral")
            card.update(kind="oral", authority=rank, authorityLabel=label, effectiveDate=None, code=None, topics=[],
                        source={"bot": ", ".join(r.get("bots") or []) or None, "utteranceId": r["anchor"]})
        card["authorityId"] = card["kind"]
        card["basis"] = card.get("basis") or topic_basis.get(topic_key(card["topic"]))
        fix = fixes.get(card["id"]) or {}
        for field in ("effectiveDate", "basis", "topic"):
            if fix.get(field):
                card[field] = str(fix[field])
        if fix.get("authority"):
            card["authorityId"] = fix["authority"]
            card["authority"], card["authorityLabel"] = rules.rank(fix["authority"])
        if fix:
            card["fixedBy"] = "decisions.yaml"
        cards.append(card)
    return sorted(cards, key=lambda c: (c["topic"], c["authority"], c["id"]))


def tables(kb, classes):
    """원장·기록 문서의 표 행 → 조회용 (카드로 만들지 않는 데이터)."""
    rows = []
    for doc_id, c in classes.items():
        if c["kind"] != "record":
            continue
        doc = kb.documents[doc_id]
        for ch in kb.by_doc.get(doc_id, []):
            for line in ch["text"].splitlines():
                if classify_mod.TABLE_LINE.match(line):
                    rows.append({"documentId": doc_id, "fileName": doc.get("fileName"),
                                 "location": ch.get("location"), "row": line.strip()})
    return rows


def write_classification(kb, classes, path):
    lines = ["# 문서 분류", "", "고칠 문서는 `overrides.yaml`에 `문서 id: 종류`로 적고 다시 빌드하세요 "
             "(종류: rule · record · promo · old · unknown · skip).", "",
             "| 문서 id | 파일 | 수집 쪽 어림 | 분류 | 권위 | 이유 | 문서 번호 · 시행일 |", "|---|---|---|---|---|---|---|"]
    for doc_id, doc in sorted(kb.documents.items(), key=lambda x: (classes[x[0]]["rank"] or 99, x[1].get("fileName"))):
        c = classes[doc_id]
        ver = " · ".join(x for x in (doc.get("code"), doc.get("effectiveDate")) if x)
        lines.append(f"| {doc_id} | {doc.get('fileName')} | {doc.get('kind')} | {c['kind']} | {c['label']} | {c['reason']} | {ver} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- 게이트와 공개 ---

def default_gate(staging, traps_args=()):
    """프롬프트 크기 + 회귀 테스트(run_traps, 기본은 가짜 LLM)를 staging 카드로 돌린다."""
    size = len((Path(staging) / "kb.txt").read_text(encoding="utf-8"))
    if size > MAX_PROMPT_CHARS:
        return False, f"카드 글이 {size:,}자로 상한 {MAX_PROMPT_CHARS:,}자를 넘음"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(ROOT / "run_traps.py"), "--kb", str(staging), *traps_args],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
    tail = (proc.stdout.strip().splitlines() or ["(출력 없음)"])[-1]
    return proc.returncode == 0, f"회귀 테스트: {tail}"


def publish(paths):
    """staging → current 교체. 새 폴더를 다 쓴 뒤 이름만 바꿔, 봇이 반쯤 쓴 카드를 읽지 않게 한다."""
    new, old = paths.out / "current.new", paths.out / "current.old"
    for p in (new, old):
        shutil.rmtree(p, ignore_errors=True)
    shutil.copytree(paths.staging, new)
    if paths.current.exists():
        paths.current.rename(old)
    new.rename(paths.current)
    shutil.rmtree(old, ignore_errors=True)


async def build(kb_path, out, extractor=None, gate=default_gate, max_units=None, log=print):
    """빌드 한 번. extractor가 None이면 추출 없이 분류·조립만 한다 (분류 확인용)."""
    paths = Paths(out)
    paths.out.mkdir(parents=True, exist_ok=True)
    kb = contract.load(kb_path)
    classes = classify_mod.classify(kb, classify_mod.load_overrides(paths.overrides))
    write_classification(kb, classes, paths.out / "classification.md")

    state = _read_json(paths.state, {"units": {}})
    raw = {r["id"]: r for r in _read_jsonl(paths.cards)}
    rejected = _read_jsonl(paths.rejected)
    stats = {"units": 0, "failed": 0, "accepted": 0, "rejected": 0}
    pending = document_units(kb, classes, state) + oral_units(kb, state)
    if extractor is not None:
        todo = pending[:max_units] if max_units else pending
        log(f"추출할 단위 {len(todo)}개 (남은 것 {len(pending)}개)")
        stats = await run_units(todo, extractor, state, raw, rejected, {r["topic"] for r in raw.values()}, log)
        _write_json(paths.state, state)
        _write_jsonl(paths.cards, list(raw.values()))
        _write_jsonl(paths.rejected, rejected)

    decisions = conflict_mod.load_decisions(paths.decisions)
    cards = assemble(kb, classes, raw, decisions)
    conflicts = conflict_mod.find(cards)
    conflict_mod.apply(cards, conflicts, decisions)
    conflict_mod.write_review(conflicts, cards, paths.out / "conflicts.md", paths.decisions)

    shutil.rmtree(paths.staging, ignore_errors=True)
    paths.staging.mkdir(parents=True)
    text = render(cards, conflicts)
    (paths.staging / "kb.txt").write_text(text, encoding="utf-8")
    _write_json(paths.staging / "cards.json", cards)
    _write_json(paths.staging / "conflicts.json", conflicts)
    _write_jsonl(paths.staging / "tables.jsonl", tables(kb, classes))
    version = {"builtAt": datetime.now().isoformat(timespec="seconds"), "source": str(kb_path),
               "exportedAt": kb.meta.get("exportedAt"), "cards": len(cards), "conflicts": len(conflicts),
               "chars": len(text)}
    _write_json(paths.staging / "version.json", version)

    ok, gate_msg = gate(paths.staging) if gate else (True, "게이트 생략")
    if ok:
        publish(paths)
    left = len(pending) - stats["units"] if extractor is not None else len(pending)
    report = {**version, **stats, "published": ok, "gate": gate_msg, "pendingUnits": left,
              "failedUnits": {k: v["error"] for k, v in state["units"].items() if v.get("status") == "failed"},
              "kinds": _count(c["kind"] for c in classes.values()), "rejectedTotal": len(rejected),
              "llmCalls": getattr(extractor, "calls", 0)}
    write_report(report, rejected, paths.out / "report.md")
    return report


def _count(items):
    out = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def write_report(r, rejected, path):
    lines = ["# kb 빌드 보고", "",
             f"- 빌드: {r['builtAt']} (kb 내보낸 시각 {r['exportedAt']})",
             f"- 문서 분류: " + ", ".join(f"{k} {v}" for k, v in sorted(r["kinds"].items())),
             f"- 이번 빌드: 단위 {r['units']}개 처리, 실패 {r['failed']}개, 카드 {r['accepted']}장 받음, "
             f"근거 없어 버림 {r['rejected']}장, LLM 호출 {r['llmCalls']}번",
             f"- 남은 단위: {r['pendingUnits']}개",
             f"- 전체 카드 {r['cards']}장, 충돌 {r['conflicts']}건, 프롬프트 글 {r['chars']:,}자",
             f"- 공개: {'예' if r['published'] else '아니오'} ({r['gate']})", ""]
    if r["failedUnits"]:
        lines += ["## 실패한 단위 (다음 빌드에서 다시 시도)", *[f"- {k}: {v}" for k, v in r["failedUnits"].items()], ""]
    if rejected:
        lines += ["## 버린 카드 (최근 10장)",
                  *[f"- {x['why']}: “{str(x['card'].get('evidence', ''))[:80]}” ({x['unit']})" for x in rejected[-10:]]]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
