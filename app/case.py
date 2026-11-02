"""평가 첫 메시지의 case metadata 블록 분리.

  case metadata
  - id: CASE-000
  - 상담 인입 일시: 2037-06-30(화) 15:16
  - 인입창구: 조합몰 1:1 채팅
  - 인입 계정: 회원 로그인 · 홍길동 · 010-0000-0000
  (빈 줄)
  고객 문의 ...

블록은 고객 발화가 아니므로 LLM에는 케이스 정보(시스템 쪽 컨텍스트)로만 넘기고, 사용자 메시지는 고객 문의 부분만 쓴다.
"""

import re

from app import calendar

HEADER = re.compile(r"^\s*case metadata\s*$", re.IGNORECASE)
FIELD = re.compile(r"^\s*-\s*([^:]+?)\s*:\s*(.*?)\s*$")
FIELD_NAMES = {"id": "id", "상담 인입 일시": "received_at", "인입창구": "channel", "인입 계정": "account"}


def parse(content):
    """(case, 고객 문의 텍스트). 블록이 없으면 case는 None, 텍스트는 그대로.

    case: {"id", "received_at", "channel", "account"} (없는 항목은 빠짐)
    """
    lines = (content or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None or not HEADER.match(lines[start]):
        return None, content
    case = {}
    end = start + 1
    while end < len(lines):
        m = FIELD.match(lines[end])
        if not m:
            break
        key = FIELD_NAMES.get(m.group(1))
        if key:
            case[key] = m.group(2)
        end += 1
    if not case.get("id"):
        return None, content
    # 케이스 시작 시점에 이관 회신 기한을 계산해 둠 (영업일·공휴일 달력 기준)
    try:
        case["escalation_deadline"] = calendar.escalation_deadline(case.get("received_at"))
    except ValueError:
        pass
    return case, "\n".join(lines[end:]).strip()


def context(case):
    """매 LLM 호출에 넣을 케이스 정보(시스템 쪽 컨텍스트)."""
    lines = ["<case>", "이번 상담의 케이스 정보 (시스템이 준 정보이며 고객 발화가 아니다)"]
    if case.get("received_at"):
        lines.append(f"- 현재 시각(상담 인입 일시): {case['received_at']}")
    if case.get("escalation_deadline"):
        lines.append(f"- 이관 회신 기한 = {case['escalation_deadline']} (인입 일시 기준 영업일 3일, 공휴일 반영)")
    if case.get("channel"):
        lines.append(f"- 인입창구: {case['channel']}")
    if case.get("account"):
        lines.append(f"- 인입 계정: {case['account']}")
    lines += [
        "",
        "- 날짜·기한·경과일은 위 현재 시각을 오늘로 보고 계산한다.",
        "- 인입 계정의 이름·전화번호는 본인 계정의 회원 검색과 대조에만 쓰고, 고객이 먼저 말하기 전에 읽어 주지 않는다.",
        "- 로그인은 계정 소유만 증명할 뿐 다른 회원의 주문을 조회·안내할 권한이 아니다. 주문번호를 안다는 것도 권한이 아니다.",
        "  조회한 주문이 인입 계정의 주문이 아니면 배송 상태·주소 등 내용을 일부라도 알려 주지 않는다.",
        "</case>",
    ]
    return "\n".join(lines)
