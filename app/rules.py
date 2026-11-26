"""우선순위 헌법 설정(rules.yaml) 읽기. 봇·kb 빌드·판정 도구가 같은 순서를 쓰도록 한곳에서 읽는다.

권위 순서는 대회 당일 바뀔 수 있어서 코드가 아니라 설정 파일에 둔다.
"""

import os
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "rules.yaml"


def path():
    return Path(os.getenv("RULES_FILE") or DEFAULT_FILE)


@lru_cache(maxsize=4)
def _load(p, mtime):
    data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    order = data.get("authority") or []
    if not order:
        raise ValueError(f"{p}: authority(권위 순서)가 비어 있습니다")
    ranks = {a["id"]: (i + 1, a["label"]) for i, a in enumerate(order)}
    unknown = data.get("unknown_as") or order[-1]["id"]
    ranks.setdefault("unknown", (ranks[unknown][0], "확인 필요"))
    bases = data.get("bases") or ["오늘"]
    return {
        "order": order, "ranks": ranks,
        "acknowledge": data.get("acknowledge") or "'{loser_value}'({loser_label})는 인정하되, 처리는 '{winner_value}'({winner_label}) 기준입니다.",
        "bases": bases, "default_basis": data.get("default_basis") or bases[-1],
        "topics": data.get("topics") or {}, "forbidden": data.get("forbidden") or [],
    }


def get():
    """설정 (파일이 바뀌면 다시 읽는다)."""
    p = path()
    return _load(str(p), p.stat().st_mtime_ns)


def rank(authority_id):
    """권위 id → (순위, 이름). 모르는 id는 맨 아래."""
    cfg = get()
    return cfg["ranks"].get(authority_id, (len(cfg["order"]) + 1, authority_id or "?"))
