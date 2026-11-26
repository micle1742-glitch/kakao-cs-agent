"""규칙 카드와 근거 확인.

카드 한 장 = 규칙 하나. 근거 문장(evidence)은 원문 조각에 실제로 있어야 한다 — LLM이 그럴듯하게 지어낸 규칙이
봇에 들어가지 않게 코드가 확인하고, 없으면 버린다 (수집 쪽의 "근거 없는 발굴은 버린다"와 같은 원칙).

카드 필드
  id           근거 위치·문장으로 만든 고정 id (다시 빌드해도 같음)
  topic        주제 이름 (충돌 찾기의 묶음 기준. 예: "하자 접수 기한")
  rule         고객 응대에 쓸 규칙 한 문장
  values       [{name, value}] 규칙 속 수치·기한·금액 (충돌 찾기의 비교 대상)
  basis        적용 기준 시점 (주문 시점·신고 시점·결제 완료 시각 등, 없으면 null)
  evidence     원문 그대로의 근거 문장
  source       {documentId, fileName, location, chunkId} 또는 구두면 {bot, utteranceId}
  kind, authority, authorityLabel, effectiveDate, code, topics
"""

import hashlib
import re

MIN_EVIDENCE = 6  # 공백을 뺀 근거 문장 최소 길이 (너무 짧으면 어디에나 있어서 근거가 못 됨)
_SPACE = re.compile(r"\s+")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "「": "", "」": "", "『": "", "』": ""})


def norm(text):
    """근거 대조용: 공백을 모두 지우고 따옴표 모양을 맞춘다 (LLM이 띄어쓰기·따옴표를 바꿔 옮기는 일이 잦음)."""
    return _SPACE.sub("", str(text or "")).translate(_QUOTES)


def card_id(anchor, evidence):
    return "K" + hashlib.sha1(f"{anchor}|{norm(evidence)}".encode("utf-8")).hexdigest()[:10]


def topic_key(topic):
    return norm(topic).lower()


def verify(card, candidates):
    """근거 문장이 후보 원문 중 하나에 있으면 그 후보의 id, 없으면 None.

    candidates: [(id, 원문)] — 카드가 가리킨 조각을 맨 앞에, 같은 문서의 다른 조각을 뒤에 둔다
    (LLM이 조각 번호를 잘못 적어도 같은 문서 안에 있으면 받아 주고 위치를 고친다).
    """
    quote = norm(card.get("evidence"))
    if len(quote) < MIN_EVIDENCE:
        return None
    for cid, text in candidates:
        if quote in norm(text):
            return cid
    return None


def clean_values(values):
    out = []
    for v in values or ():
        if isinstance(v, dict) and str(v.get("name", "")).strip() and str(v.get("value", "")).strip():
            out.append({"name": str(v["name"]).strip(), "value": str(v["value"]).strip()})
    return out
