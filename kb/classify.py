"""문서 분류와 권위.

수집 쪽 kind 어림(rule/record/promo/capture/other)을 시작점으로, 내용 단서로 한 번 보정하고, 사람이 고친
overrides 파일이 있으면 그것이 이긴다. 분류가 곧 카드의 권위(충돌 때 어느 쪽이 이기나)를 정한다.

권위 순서는 rules.yaml(우선순위 헌법)에서 읽는다. 문서 분류 → 권위 id:
  record 시스템 원장·기록 / rule 현행 규정 / old 같은 문서 번호의 옛 판 / promo 홍보물 / oral 대화로만 나온 진술
  unknown 종류를 못 정한 문서 (rules.yaml의 unknown_as와 같은 순위)
"""

import re
from pathlib import Path

import yaml

from app import rules

KINDS = ("record", "rule", "old", "unknown", "promo", "oral")


def authority(kind):
    """문서 분류 → (순위, 이름). skip 등 권위가 없는 분류는 (None, '건너뜀')."""
    return rules.rank(kind) if kind in KINDS else (None, "건너뜀")

# 내용 단서 (사진·기타로 들어온 문서를 다시 본다)
RULE_HINT = re.compile(r"제\s*\d+\s*조|규정|약관|세칙|절차서|기준|시행|부칙")
PROMO_HINT = re.compile(r"이벤트|할인|쿠폰 코드|지금 바로|무료배송|당일|라이브|배너|특가|!{1,}")
TABLE_LINE = re.compile(r".+\|.+")


def refine_kind(doc, chunks):
    """capture·other를 내용으로 규정·기록·홍보물 중 하나로 보정. 못 정하면 unknown."""
    kind = doc.get("kind") or "other"
    if kind in ("rule", "record", "promo"):
        return kind
    text = "\n".join(c["text"] for c in chunks)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    table_share = sum(1 for ln in lines if TABLE_LINE.match(ln)) / max(len(lines), 1)
    if table_share >= 0.5:
        return "record"
    rule_hits, promo_hits = len(RULE_HINT.findall(text)), len(PROMO_HINT.findall(text))
    if rule_hits >= 2 and rule_hits >= promo_hits:
        return "rule"
    if promo_hits >= 2:
        return "promo"
    return "unknown"


def code_family(code):
    """문서 번호의 판 부분을 뗀 것. RT-2037-02 → RT-2037 (같은 문서의 다른 판끼리 묶는다)."""
    return re.sub(r"[-_.]?\d+$", "", code) if code else None


def superseded_ids(documents):
    """같은 문서 번호 계열에서 시행일이 가장 늦은 판이 아닌 문서 id."""
    newest = {}
    for d in documents.values():
        fam, date = code_family(d.get("code")), d.get("effectiveDate")
        if fam and date and (fam not in newest or date > newest[fam]):
            newest[fam] = date
    return {d["id"] for d in documents.values()
            if code_family(d.get("code")) in newest and d.get("effectiveDate")
            and d["effectiveDate"] < newest[code_family(d["code"])]}


def _different_version(a, b):
    """문서 번호나 시행일이 둘 다 있고 서로 다르면 다른 판이다 (구판·현행판은 내용이 비슷해도 둘 다 남긴다)."""
    return any(a.get(f) and b.get(f) and a[f] != b[f] for f in ("code", "effectiveDate"))


def duplicates(kb, classes, threshold=0.9):
    """내용이 거의 같은 문서(같은 문서의 docx·pdf 쌍 등) → {버릴 문서 id: 남길 문서 id}.

    같은 카드가 두 번 나오고 LLM 호출이 두 배가 되는 것을 막는다. 남기는 쪽: 권위가 강한 쪽 → 문서 번호·시행일이
    있는 쪽 → 조각이 많은 쪽. 공백을 뺀 글로 비교하고, 길이가 20% 넘게 다르면 다른 문서로 본다.
    """
    from difflib import SequenceMatcher

    from kb.cards import norm
    texts = {d: norm(kb.doc_text(d)) for d in kb.documents}
    order = sorted(kb.documents, key=lambda d: ((classes[d]["rank"] or 99), not kb.documents[d].get("code"),
                                                 -len(kb.by_doc.get(d, ())), d))
    keep, out = [], {}
    for d in order:
        a = texts[d]
        if len(a) < 20:
            keep.append(d)
            continue
        for k in keep:
            b = texts[k]
            if not b or abs(len(a) - len(b)) > 0.2 * max(len(a), len(b)) or _different_version(
                    kb.documents[d], kb.documents[k]):
                continue
            m = SequenceMatcher(None, a, b, autojunk=False)
            if m.real_quick_ratio() >= threshold and m.quick_ratio() >= threshold and m.ratio() >= threshold:
                out[d] = k
                break
        else:
            keep.append(d)
    return out


def load_overrides(path):
    """사람이 고친 분류: {문서 id: kind} (kind는 rule·record·promo·old·unknown·skip)."""
    if not path or not Path(path).exists():
        return {}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k): v for k, v in data.items()}


def classify(kb, overrides=None):
    """{문서 id: {"kind", "rank", "label", "reason"}}."""
    overrides = overrides or {}
    old = superseded_ids(kb.documents)
    out = {}
    for doc_id, doc in kb.documents.items():
        if doc_id in overrides:
            kind, reason = overrides[doc_id], "사람이 정함"
        elif doc_id in old:
            kind, reason = "old", "같은 문서 번호의 더 새 판이 있음"
        else:
            kind = refine_kind(doc, kb.by_doc.get(doc_id, []))
            reason = "수집 쪽 어림" if kind == doc.get("kind") else f"내용으로 보정 ({doc.get('kind')}→{kind})"
        rank, label = authority(kind)
        out[doc_id] = {"kind": kind, "rank": rank, "label": label, "reason": reason, "sameAs": None}
    for dup, keep in duplicates(kb, out).items():
        if dup not in overrides:  # 사람이 따로 정한 문서는 그대로
            out[dup]["sameAs"] = keep
            out[dup]["reason"] += f" · 내용이 {kb.documents[keep].get('fileName')}와 같아 추출 생략"
    return out
