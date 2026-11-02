"""박스·세트 상품 일부 하자의 부분환불 계산.

하자 비율 R = 하자 개체 수 ÷ 입수 × 100, 소수 첫째 자리 반올림, 구간 판정은 반올림한 값으로 한다.
R < 20%: 실결제액 × 하자 수 ÷ 입수 / 20% ≤ R ≤ 80%: 실결제액 40% 또는 전량 재배송 / R > 80%: 전액환불 또는 전량 재배송.
환불액은 10원 단위 절상, 500원 미만이면 500원(실결제액을 넘지 않게).
(구간·비율·금액은 예시 값이다. 실제 규정은 대회 자료라 비공개)
"""

from decimal import ROUND_HALF_UP, Decimal

MIN_REFUND = 500  # 예시 값 (실제 규정은 대회 자료라 비공개)
PARTIAL_BAND_MAX = 20  # 예시 값 (실제 규정은 대회 자료라 비공개) — 이 비율(%) 미만은 하자분 비례 환불
FULL_BAND_MIN = 80  # 예시 값 (실제 규정은 대회 자료라 비공개) — 이 비율(%) 초과는 전액환불
MIDDLE_REFUND_PERCENT = 40  # 예시 값 (실제 규정은 대회 자료라 비공개) — 중간 구간 환불 비율(%)


def defect_ratio(defects, count):
    """하자 비율(%)을 소수 첫째 자리로 반올림한 Decimal."""
    return (Decimal(defects) * 100 / Decimal(count)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _ceil10(numerator, denominator):
    """numerator ÷ denominator를 10원 단위로 절상."""
    return -(-numerator // (denominator * 10)) * 10


def partial_refund(paid, defects, count):
    """(실결제액, 하자 개체 수, 입수) → {"ratio", "band", "refund", "options"}."""
    paid, defects, count = int(paid), int(defects), int(count)
    if count <= 0 or paid <= 0 or not 0 < defects <= count:
        raise ValueError("실결제액·입수는 0보다 크고 하자 개체 수는 1 이상 입수 이하여야 합니다")
    ratio = defect_ratio(defects, count)
    if ratio < PARTIAL_BAND_MAX:
        band, refund = f"부분 하자 (R < {PARTIAL_BAND_MAX}%)", _ceil10(paid * defects, count)
    elif ratio <= FULL_BAND_MIN:
        band, refund = (f"상당 부분 하자 ({PARTIAL_BAND_MAX}% ≤ R ≤ {FULL_BAND_MIN}%)",
                        _ceil10(paid * MIDDLE_REFUND_PERCENT, 100))
    else:
        band, refund = f"사실상 전부 하자 (R > {FULL_BAND_MIN}%)", paid
    refund = min(max(refund, MIN_REFUND), paid)
    if ratio < PARTIAL_BAND_MAX:
        options = [f"부분환불 {refund:,}원"]
    elif ratio <= FULL_BAND_MIN:
        options = [f"부분환불 {refund:,}원 (실결제액 {MIDDLE_REFUND_PERCENT}%)", "전량 재배송"]
    else:
        options = [f"전액환불 {refund:,}원 (배송비 부담 없음)", "전량 재배송 (배송비 부담 없음)"]
    return {"ratio": f"{ratio}%", "band": band, "refund": refund, "options": options}
