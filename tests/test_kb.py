"""kb 빌드 시험 — 지어낸 예시 kb(tests/fixtures/kb)와 가짜 추출기로, LLM 없이 돈다."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml

from kb import build as kb_build
from kb import classify, contract
from kb.cards import verify
from kb.extract import ScriptedExtractor

FIX = Path(__file__).resolve().parent / "fixtures" / "kb"
SAMPLE = FIX / "sample.jsonl"


def script():
    return yaml.safe_load((FIX / "script.yaml").read_text(encoding="utf-8"))


def run_build(out, extractor, gate=lambda staging: (True, "시험")):
    return asyncio.run(kb_build.build(SAMPLE, out, extractor, gate=gate, log=lambda *_: None))


def cards_of(out):
    return json.loads((Path(out) / "current" / "cards.json").read_text(encoding="utf-8"))


def test_load_and_classify():
    kb = contract.load(SAMPLE)
    assert len(kb.documents) == 7 and len(kb.findings) == 1 and len(kb.utterances) == 3
    kinds = {k: v["kind"] for k, v in classify.classify(kb).items()}
    assert kinds["d-old"] == "old"            # 같은 문서 번호(RT-2037)의 더 새 판이 있음
    assert kinds["d-capture"] == "rule"       # 사진이지만 조문·시행 단서로 규정으로 보정
    assert kinds["d-promo"] == "promo" and kinds["d-record"] == "record"
    assert kinds["d-empty"] == "unknown"


def test_duplicate_copy_is_skipped_but_versions_are_kept():
    kb = contract.load(SAMPLE)
    classes = classify.classify(kb)
    assert classes["d-rule-pdf"]["sameAs"] == "d-rule"   # 같은 규정의 pdf 사본은 추출 생략
    assert classes["d-old"]["sameAs"] is None             # 구판은 내용이 비슷해도 남긴다


def test_unsupported_version(tmp_path):
    p = tmp_path / "kb.jsonl"
    p.write_text('{"type":"meta","version":2}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        contract.load(p)


def test_verify_ignores_spacing_and_fixes_location():
    card = {"evidence": "하자 신고는  수령일로부터 “3일” 이내"}
    segs = [("a:0", "다른 문장입니다"), ("a:1", "하자 신고는 수령일로부터 \"3일\" 이내에 해 주세요.")]
    assert verify(card, segs) == "a:1"
    assert verify({"evidence": "원문에 없는 규칙 문장"}, segs) is None
    assert verify({"evidence": "하자"}, segs) is None  # 너무 짧은 근거는 받지 않음


def test_build_cards_conflicts_and_failures(tmp_path):
    extractor = ScriptedExtractor(script())
    report = run_build(tmp_path, extractor)
    cards = cards_of(tmp_path)
    assert "d-rule-pdf" not in extractor.seen            # 같은 내용의 사본은 LLM에 보내지 않음
    assert report["published"] and len(cards) == 7
    assert report["rejected"] == 1                       # 지어낸 근거 문장은 버림
    assert "d-capture" in report["failedUnits"]          # 실패한 문서는 빼고 진행
    old = next(c for c in cards if c["source"].get("documentId") == "d-old")
    assert old["source"]["chunkId"] == "d-old:0"         # 잘못 적은 조각 번호를 고침
    assert old["authorityLabel"] == "구판·인쇄물" and old["authorityId"] == "old"
    oral = [c for c in cards if c["kind"] == "oral"]
    assert {c["topic"] for c in oral} == {"냉동 온도 이탈", "출고 시점"}   # 발굴 + 걸러진 발화 1개
    conflicts = json.loads((tmp_path / "current" / "conflicts.json").read_text(encoding="utf-8"))
    assert {c["suggestion"] for c in conflicts} == {"5일", "월·수·금"}
    assert (tmp_path / "decisions.yaml").exists() and (tmp_path / "classification.md").exists()


def test_incremental_only_new_units(tmp_path):
    run_build(tmp_path, ScriptedExtractor(script()))
    fixed = script()
    fixed["d-capture"] = [{"topic": "포장", "rule": "냉동 상품은 아이스팩 두 개를 넣는다.", "values": [],
                           "evidence": "냉동 상품은 아이스팩 두 개를 넣는다.", "chunkId": "d-capture:0"}]
    second = ScriptedExtractor(fixed)
    report = run_build(tmp_path, second)
    assert second.seen == ["d-capture"]                  # 끝난 단위는 다시 보내지 않음
    assert report["failedUnits"] == {} and len(cards_of(tmp_path)) == 8


def test_decision_marks_loser(tmp_path):
    run_build(tmp_path, ScriptedExtractor(script()))
    decisions = yaml.safe_load((tmp_path / "decisions.yaml").read_text(encoding="utf-8"))
    cid = next(k for k, v in decisions.items() if "접수 기한" in v["note"])
    decisions[cid]["choose"] = "suggested"
    (tmp_path / "decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True), encoding="utf-8")
    run_build(tmp_path, None)                            # LLM 없이 판정만 반영
    old = next(c for c in cards_of(tmp_path) if c["source"].get("documentId") == "d-old")
    assert old["status"] == "인정만"
    assert "(근거로 쓰지 않음) 하자 신고는 수령일로부터 3일" in (tmp_path / "current" / "kb.txt").read_text(encoding="utf-8")


def test_override_skip(tmp_path):
    (tmp_path / "overrides.yaml").write_text("d-promo: skip\n", encoding="utf-8")
    run_build(tmp_path, ScriptedExtractor(script()))
    assert not any(c["source"].get("documentId") == "d-promo" for c in cards_of(tmp_path))


def test_gate_failure_keeps_current(tmp_path):
    run_build(tmp_path, ScriptedExtractor(script()))
    before = (tmp_path / "current" / "version.json").read_text(encoding="utf-8")
    report = run_build(tmp_path, None, gate=lambda staging: (False, "회귀 테스트 실패(시험)"))
    assert not report["published"]
    assert (tmp_path / "current" / "version.json").read_text(encoding="utf-8") == before


def test_bot_reloads_new_kb_without_restart(tmp_path, monkeypatch):
    from app import prompt
    monkeypatch.setattr(prompt, "KB_DIR", tmp_path / "current")
    monkeypatch.setattr(prompt, "_cache", {"stamp": "unset", "prompt": None})
    assert "<kb_topics>" not in prompt.system_prompt()  # 아직 공개본 없음
    run_build(tmp_path, ScriptedExtractor(script()))
    assert "하자 접수 기한" in prompt.system_prompt()     # 시스템 프롬프트엔 쟁점 목록 (재시작 없이 반영)
    assert "수령일로부터 5일" in prompt.kb_context("하자 접수 기한이 며칠이에요?")  # 카드 본문은 상황별로
    monkeypatch.setenv("KB_SELECT", "off")
    prompt._cache["stamp"] = "unset"
    assert "하자 신고는 수령일로부터 5일" in prompt.system_prompt()  # 끄면 예전처럼 전부


def test_real_gate_runs_traps(tmp_path):
    """기본 게이트: staging 카드로 회귀 테스트(가짜 LLM 예시 TC)를 실제로 돌린다."""
    report = run_build(tmp_path, ScriptedExtractor(script()), gate=kb_build.default_gate)
    assert report["published"], report["gate"]
    assert "TC 통과" in report["gate"]


def test_basis_default_and_human_card_fix(tmp_path, monkeypatch):
    """기준 시점은 rules.yaml 쟁점 기본값으로 채우고, 문서에서 못 찾은 값은 decisions.yaml cards:로 사람이 채운다."""
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "rules.yaml").read_text(encoding="utf-8"))
    cfg["topics"] = {"하자 접수 기한": "신고일"}
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("RULES_FILE", str(rules_file))
    out = tmp_path / "kb"
    run_build(out, ScriptedExtractor(script()))
    promo = next(c for c in cards_of(out) if c["source"].get("documentId") == "d-promo")
    assert next(c for c in cards_of(out) if c["topic"] == "하자 접수 기한")["basis"] == "신고일"
    decisions = yaml.safe_load((out / "decisions.yaml").read_text(encoding="utf-8")) or {}
    decisions["cards"] = {promo["id"]: {"effectiveDate": "2037-06-08", "authority": "oral"}}
    (out / "decisions.yaml").write_text(yaml.safe_dump(decisions, allow_unicode=True), encoding="utf-8")
    run_build(out, None)
    fixed = next(c for c in cards_of(out) if c["id"] == promo["id"])
    assert fixed["effectiveDate"] == "2037-06-08" and fixed["authorityId"] == "oral"
    assert fixed["authorityLabel"] == "구두" and fixed["fixedBy"] == "decisions.yaml"
