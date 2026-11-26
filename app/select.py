"""카드 골라 넣기: kb 카드 전부가 아니라 이번 상황에 맞는 몇 장만 프롬프트에 넣는다.

카드가 수백 장이 되면 전부 넣는 프롬프트는 느리고(p95), 모델이 맞는 카드를 놓친다. 그래서
  - 시스템 프롬프트(매번 같아서 캐시됨): 헌법 + 규칙 + 쟁점 이름 목록만
  - 매 턴 컨텍스트: 고객 말과 가장 가까운 주제 묶음의 카드 2~5장
으로 나눈다. 판·권위 판정(judge_rule)은 프롬프트와 상관없이 전체 카드를 보므로 골라 넣기가 판정을 바꾸지 않는다.

점수: 공백을 뺀 글의 글자쌍(바이그램)이 겹치는 정도를 드문 글자쌍일수록 크게(IDF). 주제 이름이 겹치면 3배,
이번 말은 앞선 말의 2배. 한국어는 조사·어미가 붙어 단어 단위로는 잘 안 맞아서 글자쌍을 쓴다.

한계: 같은 뜻을 다른 말로 하면 놓친다 ("문자 링크로 결제" vs 카드의 "피싱"). 그래서 쟁점 이름 목록은 항상
시스템 프롬프트에 두고, 모델이 judge_rule로 어떤 쟁점이든 값을 받아 올 수 있게 한다. 위기·피싱은 코드가 따로 지킨다.
"""

import math
import re

MIN_CARDS, MAX_CARDS = 2, 5
_SPACE = re.compile(r"\s+")


def bigrams(text):
    t = _SPACE.sub("", str(text or "")).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _card_text(card):
    return " ".join([card.get("rule", "")] + [f"{v['name']} {v['value']}" for v in card.get("values") or ()])


def score_topics(cards, current, earlier=()):
    """{주제: 점수}. current = 이번 고객 말, earlier = 이 케이스의 앞선 고객 말들.

    글자쌍마다 '드문 정도'(IDF = log(주제 수 / 그 글자쌍이 나온 주제 수))로 가중한다. "주문·결제·수박"처럼
    거의 모든 카드에 나오는 말이 점수를 독차지하지 않고, "피싱·사칭·위탁"처럼 드문 말이 주제를 가른다.
    """
    now, before = bigrams(current), bigrams(" ".join(earlier))
    groups = {}
    for c in cards:
        groups.setdefault(c["topic"], []).append(c)
    grams = {topic: (bigrams(topic), set().union(*(bigrams(_card_text(c)) for c in group)))
             for topic, group in groups.items()}
    df = {}
    for t, body in grams.values():
        for g in t | body:
            df[g] = df.get(g, 0) + 1
    n = max(len(groups), 1)

    def weight(common):
        return sum(math.log((n + 1) / (df.get(g, 0) + 0.5)) for g in common if not g.isdigit())

    scores = {}
    for topic, (t, body) in grams.items():
        scores[topic] = (2 * (3 * weight(t & now) + weight(body & now))
                         + (3 * weight(t & before) + weight(body & before)))
    return scores, groups


def pick(cards, current, earlier=(), min_cards=MIN_CARDS, max_cards=MAX_CARDS):
    """점수가 높은 주제 묶음부터 카드를 담는다. 묶음째 담아(판·충돌 카드가 같이 보이게) max_cards를 넘기지 않되,
    점수가 0인 주제는 넣지 않는다. 첫 묶음이 max_cards보다 크면 권위가 강한 카드부터 자른다."""
    scores, groups = score_topics(cards, current, earlier)
    chosen = []
    for topic in sorted(scores, key=lambda t: (-scores[t], t)):
        if scores[topic] <= 0 or len(chosen) >= max_cards:
            break
        group = sorted(groups[topic], key=lambda c: (c.get("status") == "인정만", c.get("authority") or 99))
        room = max_cards - len(chosen)
        if len(group) > room and len(chosen) >= min_cards:
            continue  # 다 못 담는 묶음은 건너뛰고 더 작은 묶음을 찾는다
        chosen += group[:room]
    return chosen
