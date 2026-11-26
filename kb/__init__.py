"""지식 넣는 틀(kb 빌드): 정보 수집 쪽이 내보낸 kb.jsonl을 규칙 카드·데이터 표로 바꿔 봇에 넣는다.

흐름: 읽기(contract) → 문서 분류(classify) → 카드 뽑기(extract) + 근거 확인(cards) → 구두 카드(oral)
     → 충돌 찾기(conflicts) → 작업 캐시(work/) → 회귀 테스트 통과 시 공개본(current/) 교체 → 봇이 다시 읽음.
형식 약속: kb.jsonl version 1 (document · chunk · finding · utterance · person 줄).
"""
