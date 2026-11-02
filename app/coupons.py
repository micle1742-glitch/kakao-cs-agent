"""보상 쿠폰 계산.

가격 인하 차액 보상: 차액 이상이 되는 최소 금액 한 장 (2,000원 이하 → DEMO2000 · 2,001~4,000원 → DEMO4000 ·
4,001~8,000원 → DEMO8000). 차액이 8,000원을 넘으면 남은 차액이 8,000원 이하가 될 때까지 DEMO8000을 더하고,
남은 차액에 같은 규칙을 적용한다. 차액을 넘는 부분은 정산하지 않는다.
(금액·코드는 예시 값이다. 실제 규정은 대회 자료라 비공개)
"""

# 예시 값 (실제 규정은 대회 자료라 비공개)
CARE_COUPONS = [(2000, "DEMO2000"), (4000, "DEMO4000"), (8000, "DEMO8000")]


def care_coupons(diff):
    """가격 인하 차액(원) → 발급할 보상 쿠폰 코드 목록. 차액이 0 이하이면 빈 목록."""
    diff = int(diff)
    if diff <= 0:
        return []
    coupons = []
    while diff > 8000:  # 예시 값 (실제 규정은 대회 자료라 비공개) — 가장 큰 쿠폰 금액
        coupons.append("DEMO8000")
        diff -= 8000
    coupons.append(next(code for amount, code in CARE_COUPONS if diff <= amount))
    return coupons
