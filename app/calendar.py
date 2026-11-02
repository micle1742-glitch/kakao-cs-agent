"""영업일·공휴일 달력 (2037) 계산. 정본은 knowledge/business_calendar.md.

- 영업일: 월~금, 공휴일(대체공휴일 포함) 제외
- add_business_days: 당일 불산입, 기준일이 휴무일이면 다음 영업일을 기준일로 바꾼 뒤 N번째 영업일 (그날 24:00이 기한)
- subscription_cutoff: 결제일 직전 영업일을 1로 거꾸로 세어 N번째 영업일 24:00
- is_shipping_day: 산지 출고 요일(월·수·금)이면서 영업일. 출고 요일이 휴무면 그 회차는 건너뛴다
- next_shipping_day: 기준 시각 다음의 출고일 (당일 불산입)
- pickup_deadline: 픽업 예약 마감 = 수령 희망일의 1영업일 전 24:00
"""

import re
from datetime import date, timedelta

HOLIDAYS = {
    date(2037, m, d) for m, d in [
        (5, 6), (5, 25), (6, 6), (8, 15), (8, 17), (9, 23), (9, 24), (9, 25),
        (10, 3), (10, 5), (10, 9), (12, 25),
    ]
}
DEFAULT_YEAR = 2037
WEEKDAYS = "월화수목금토일"
SHIPPING_WEEKDAYS = (0, 2, 4)  # 월·수·금

_DATE = re.compile(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})")


def parse_date(value):
    """date, "2037-06-30", "2037-06-30(화) 15:16", "06-30"(2037년으로 봄)을 date로. 못 읽으면 ValueError."""
    if isinstance(value, date):
        return value
    m = _DATE.search(str(value))
    if not m:
        raise ValueError(f"날짜를 읽을 수 없음: {value!r}")
    return date(int(m.group(1) or DEFAULT_YEAR), int(m.group(2)), int(m.group(3)))


def fmt(day):
    """2037-07-03(금)"""
    return f"{day.isoformat()}({WEEKDAYS[day.weekday()]})"


def is_business_day(day):
    day = parse_date(day)
    return day.weekday() < 5 and day not in HOLIDAYS


def is_shipping_day(day):
    day = parse_date(day)
    return day.weekday() in SHIPPING_WEEKDAYS and is_business_day(day)


def add_business_days(base, n):
    """기준일로부터 N번째 영업일 (당일 불산입). 기준일이 휴무일이면 다음 영업일을 기준일로 본다."""
    day = parse_date(base)
    while not is_business_day(day):
        day += timedelta(days=1)
    left = n
    while left > 0:
        day += timedelta(days=1)
        if is_business_day(day):
            left -= 1
    return day


def subscription_cutoff(pay_date, n=3):
    """정기배송 컷오프 날짜: 결제일 직전 영업일을 1로 거꾸로 N번째 영업일 (그날 24:00)."""
    day = parse_date(pay_date)
    left = n
    while left > 0:
        day -= timedelta(days=1)
        if is_business_day(day):
            left -= 1
    return day


def next_shipping_day(base):
    """기준 시각 다음의 산지 출고일 (당일 불산입 — 오늘 들어온 주문은 빨라야 다음 출고일에 나간다).

    출고 요일(월·수·금)이 휴무일이면 그 회차는 건너뛴다 (다음 영업일로 미루지 않음).
    """
    day = parse_date(base) + timedelta(days=1)
    while not is_shipping_day(day):
        day += timedelta(days=1)
    return day


def pickup_deadline(pickup_date):
    """산지 픽업 예약 마감: 수령 희망일의 1영업일 전 (그날 24:00). 수령 희망일은 영업일이어야 한다."""
    return subscription_cutoff(pickup_date, 1)


def escalation_deadline(received_at):
    """이관 회신 기한 "YYYY-MM-DD(요일) 24:00" (인입 일시 기준 영업일 3일)."""
    return f"{fmt(add_business_days(received_at, 3))} 24:00"
