"""kb.jsonl 읽기 (형식 약속 version 1).

한 줄에 JSON 하나, 줄마다 type: meta · document · chunk · finding · utterance · person.
id는 내용 기반이라 다시 내보내도 같다 → 증분 빌드는 id로 '새로 생긴 것'만 고른다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_VERSION = 1


@dataclass
class KB:
    meta: dict = field(default_factory=dict)
    documents: dict = field(default_factory=dict)   # id -> document
    chunks: dict = field(default_factory=dict)      # id -> chunk
    by_doc: dict = field(default_factory=dict)      # document id -> [chunk] (번호 순)
    findings: list = field(default_factory=list)
    utterances: list = field(default_factory=list)
    persons: list = field(default_factory=list)

    def doc_text(self, doc_id):
        return "\n".join(c["text"] for c in self.by_doc.get(doc_id, ()))


def _chunk_no(chunk):
    tail = str(chunk["id"]).rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def load(path):
    kb = KB()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            raise ValueError(f"{path}:{n} JSON이 아닙니다: {e}") from None
        kind = row.get("type")
        if kind == "meta":
            if row.get("version") != SUPPORTED_VERSION:
                raise ValueError(f"kb.jsonl version {row.get('version')}은 지원하지 않습니다 (지원: {SUPPORTED_VERSION})")
            kb.meta = row
        elif kind == "document":
            kb.documents[row["id"]] = row
        elif kind == "chunk":
            kb.chunks[row["id"]] = row
            kb.by_doc.setdefault(row["documentId"], []).append(row)
        elif kind == "finding":
            kb.findings.append(row)
        elif kind == "utterance":
            kb.utterances.append(row)
        elif kind == "person":
            kb.persons.append(row)
        # 모르는 type은 건너뜀 (약속이 늘어나도 읽기는 멈추지 않게)
    for chunks in kb.by_doc.values():
        chunks.sort(key=_chunk_no)
    return kb
