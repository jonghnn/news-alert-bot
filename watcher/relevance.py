"""기사를 점수로 매겨 고르는 모듈.

## 기준: "차트에 나오는 건 보내지 않는다"

사용자 기준 (2026-10-26):

    "코인 올랐다 떨어졌다는 오히려 필요없어. 어차피 내가 차트를 볼건데 그게 뭔 상관이야.
     오히려 서산교육지원청이 생성형 AI로 뭘 활용해서 뭘 만들었는지가 더 궁금한데?"

가격·등락은 **차트와 호가창에 실시간으로 있다.** 뉴스로 받아도 정보가 늘지 않는다.
뉴스가 유일하게 줄 수 있는 것은 차트에 안 나오는 것 — **누가 무엇을 만들었고,
어디에 쓰였고, 무슨 규칙이 바뀌었나**다. 그래서 점수를 이렇게 준다:

    시세·등락·청산·지지선·가격 수치   →  크게 깎는다
    활용·도입·구축·출시·공개·실증     →  올린다
    기업·종목명, 기술어, 정책·규제     →  올린다

⚠️ 첫 구현(같은 날 오전)은 이것을 **정반대로** 했다. 시세 기사에 +18 을 주고
   "서산교육지원청, 생성형 AI 활용해 학교 행정업무 부담 줄인다" 에 -15 를 줬다.
   "AI·주식·코인 투자" 라는 관심사를 '시장 가격 뉴스' 로 혼자 해석한 결과였다.
   관심사를 추측해서 가중치를 정하지 말 것 — 틀리면 정확히 거꾸로 간다.

## 왜 점수가 필요한가 (실측 2026-10-26, 386건 풀)

| 소스 | 통과 | 통과율 | filter |
|---|---|---|---|
| 경제 이슈 | 69/70 | 98.6% | exclude |
| 기술 이슈 | 31/31 | 100% | exclude |
| AI 뉴스 | 100/100 | 100% | exclude(자동) |
| 코인 뉴스 | 100/100 | 100% | exclude(자동) |

6개 소스 중 4개가 include 를 안 본다. 321건이 통과하고 거기서 '최신 20건' 을
뽑았다. '최신' 이라는 기준에는 주제가 없어서 서머타임·추락사고가 섞여 왔다.

제외어를 늘리는 방향은 실측하고 버렸다 — 후보 38개 중 각 1~2건만 걸렸고,
'브리핑' 은 쓸만한 기사를 지웠고, '기부' 는 "찾기부터" 에 걸렸다(한글은 부분
문자열로 찾으므로 짧은 단어가 위험하다).

깔때기(소거법·키워드)는 그대로 두고 **마지막 선택만** 점수로 바꿨다. 넓게 받는
것과 정밀하게 고르는 것은 서로 싸우지 않는다.

⚠️ 점수는 **제목만** 본다. news 소스의 summary 에는 매체명이 들어있어서
   (예: '코인리더스') 함께 보면 매체명이 점수를 올려버린다. GeekNews 는 summary 에
   본문 설명이 들어있는데 거기 'AI' 가 한 번 나오면 "Vim 8.x 포크", "맥 잠자기
   방지 도구" 가 통과했다(실측 18건 중 대부분). 제목만 보면 5건.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_LATIN_ONLY = re.compile(r"[A-Za-z0-9]+")

# ── 깎는 신호: 차트·호가창에 이미 있는 것 ─────────────────────────────
# 이 단어들이 제목에 있으면 그 기사는 '지금 값이 얼마다' 를 말하는 기사다.
# 실측으로 한 전송에 이런 게 3건씩 들어왔다:
#   · 업비트 하락 종목 271개…비트코인·리플 약세
#   · [속보] 비트코인, 다시 8만달러 밑으로
#   · [오후 시세브리핑] 비트코인 79,398달러, 이더리움 2,302달러
# 세 건이 같은 사건(오늘 코인 하락)인데 겹치는 단어가 없어 중복 제거도 못 잡는다.
PRICE = """
돌파 하회 지지선 저항선 청산 롱숏 MVRV 상관계수 보합 반등 하락세 상승세 약세 강세
등락 낙폭 상승폭 시세 종가 장중 급등 급락 폭락 폭등 최고가 최저가 저점 고점
유통량 소각량 거래대금 무기한 확률 평가액 목표가 횡보 조정 저항 지지
""".split()

# 가격 보도의 전형적 형태. "비트코인 79,398달러", "8만 달러 회복", "1.294달러"
#
# ⚠️ % · 퍼센트 · 포인트 · 원 은 넣지 않는다. 실측에서 이것들이 정책 기사를 깎았다:
#      · 트럼프 "미국 이용하려는 기업, 관세 50%까지 올릴 수도"   ← 관세율
#      · 앨비스 프레슬리 노래 무단 학습 "1곡당 2억 원" 앤트로픽 피소   ← 소송 청구액
#    퍼센트가 시세인지 정책인지는 숫자로 알 수 없다. 그 판단은 PRICE 단어
#    (하락세·급등·지지선…)가 이미 하고 있으므로 중복해서 깎을 필요가 없다.
_PRICE_NUMBER = [
    re.compile(r"\d[\d,\.]*\s*달러"),
    re.compile(r"\d+만\s*달러"),
]

# ── 올리는 신호: 차트에 안 나오는 것 ──────────────────────────────────
# "무엇을 만들었나 / 어디에 썼나". 사용자가 가장 궁금해한 것이 이것이다.
USECASE = """
활용 도입 구축 적용 개발 공개 출시 선보 탑재 구현 시범 실증 전환 자동화 효율 절감
해결 제작 설계 배포 공급 협력 계약 수주 증설 인수 합병 진출 특허 시연 실험
서비스 플랫폼 솔루션 기능 성능 사례 성과
""".split()

TICKER = """
엔비디아 삼성전자 SK하이닉스 하이닉스 마이크론 TSMC AMD 인텔 브로드컴 ASML 퀄컴
애플 테슬라 구글 메타 아마존 마이크로소프트 오픈AI OpenAI 앤트로픽 Anthropic 팔란티어
삼성전기 LG이노텍 LG에너지솔루션 삼성SDI 네이버 카카오 현대차 기아 포스코 셀트리온
한미반도체 두산 한화 HD현대 리플 업비트 빗썸 코인베이스 젠슨 올트먼 머스크 파월
""".split()

TECH = """
반도체 HBM GPU 파운드리 D램 낸드 데이터센터 LLM 온디바이스 휴머노이드 로봇 자율주행
배터리 전기차 양자 AI반도체 가속기 에이전트 생성형 추론 모델 오픈소스 엑시노스
스냅드래곤 메모리 칩 알고리즘 데이터셋 파인튜닝 멀티모달
""".split()

# 규칙이 바뀌는 것도 차트에 안 나온다. 코인은 시세보다 이쪽이 중요하다.
POLICY = """
규제 과세 법안 제도 승인 허가 금지 제재 관세 정책 가이드라인 조례 소송 피소
개정 시행 유예 상장폐지 공시 심사
""".split()

MACRO = """
금리 기준금리 연준 FOMC 국채 물가 인플레이션 무역 수출 공급망 GDP 실업률
실적 영업이익 점유율 매출
""".split()

# ── 깎는 신호: 투자·기술과 무관한 생활 기사 ───────────────────────────
# ⚠️ 여기에 관공서·학교·대학을 넣지 않는다. 첫 구현에서 넣었다가
#    "서산교육지원청, 생성형 AI 활용해…" 를 -15 점으로 만들었다 — 사용자가
#    가장 보고 싶다고 한 기사다. 공공기관의 AI 활용은 활용 사례이지 노이즈가 아니다.
JUNK = """
운세 부고 맛집 여행 음료 추락 보이스피싱 집값 전셋값 아파트 재건축 분양 오피스텔
전세 임대 이어폰 폴더블 내구성 쿠폰 상품권 알바 월급 봉사 장학 수상 시상 위촉
임명 선임 총회 박람회 엑스포 경진대회 공모전 캠프 발대 기념
""".split()

# 제약사 사업 기사. 'AI 로 암 조직 분석' 같은 의료 AI 는 활용 사례이므로 넣지 않고,
# 순수 제약 영업·임상 용어만 넣는다 (구글 '경제' 섹션에 보도자료가 대량으로 온다).
PHARMA = "제균 항암제 약가 처방 헬리코박터 삼제요법 임상시험 신약허가 비만 치료제".split()

SIGNALS: list[tuple[list[str], int]] = [
    (USECASE, 3), (TICKER, 3), (TECH, 3), (POLICY, 3), (MACRO, 2),
    (PRICE, -5), (JUNK, -5), (PHARMA, -5),
]

# 자동번역 기사는 제목이 평서문으로 끝난다. 한국 기사 제목은 그렇지 않다.
#   "동남아시아에서 인공지능(AI) 수요가 급증하고 있습니다."
#   "인공지능의 오용은 학생들의 독립적인 사고력을 저해할 수 있다."
_TRANSLATED = re.compile(r"(습니다|됩니다|입니다|있다|이다)\.?\s*$")

# ── 주제 분류 ─────────────────────────────────────────────────────────
# 할당(한 주제가 상한을 독차지하지 않게)과 메시지 묶기에 같은 분류를 쓴다.
TOPIC_USECASE = "AI 활용·사례"
TOPIC_INDUSTRY = "AI·반도체 산업"
TOPIC_MARKET = "주식·거시"
TOPIC_COIN = "코인"
TOPIC_OTHER = "기타"
TOPIC_ORDER = [TOPIC_USECASE, TOPIC_INDUSTRY, TOPIC_MARKET, TOPIC_COIN, TOPIC_OTHER]

_AI_WORDS = set(TECH) | {
    "AI", "인공지능", "엔비디아", "오픈AI", "OpenAI", "앤트로픽", "Anthropic",
    "삼성전자", "SK하이닉스", "하이닉스", "마이크론", "TSMC", "AMD", "인텔",
    "브로드컴", "ASML", "퀄컴", "삼성전기", "LG이노텍", "챗GPT", "제미나이", "클로드",
}

_COIN_WORDS = set("""
비트코인 이더리움 리플 XRP 솔라나 도지 알트코인 스테이블코인 가상자산 암호화폐 코인
업비트 빗썸 바이낸스 코인베이스 온체인 디파이 NFT
""".split())

_MARKET_WORDS = set("""
코스피 코스닥 나스닥 다우 S&P500 S&P 닛케이 금리 기준금리 연준 FOMC 관세 국채 물가
환율 유가 원유 무역 수출 실적 영업이익 시총 자사주 배당 수주 공모주 공매도 점유율
인플레이션 GDP 실업률 매출
""".split())

# "무엇을 만들었나" 를 가리키는 동사. 이게 있으면 산업 동향이 아니라 활용 사례다.
_BUILT = set("""
활용 도입 구축 적용 개발 공개 출시 선보 탑재 구현 시범 실증 전환 자동화 절감
제작 설계 배포 시연 특허 해결
""".split())


def _pattern(word: str) -> re.Pattern | None:
    """영문·숫자 키워드만 단어 경계를 요구한다 (filters.py 와 같은 이유)."""
    if _LATIN_ONLY.fullmatch(word):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)
    return None


def _found(word: str, text: str, text_lower: str) -> bool:
    pattern = _pattern(word)
    if pattern is not None:
        return pattern.search(text) is not None
    return word.lower() in text_lower


def score(title: str, bonus=(), penalty=()) -> int:
    """제목 하나의 점수. 높을수록 먼저 보낸다."""
    lower = title.lower()
    total = 0
    for words, weight in SIGNALS:
        for word in words:
            if _found(word, title, lower):
                total += weight
    for word in bonus:                      # config 의 relevance.add
        if _found(word, title, lower):
            total += 4
    for word in penalty:                    # config 의 relevance.penalize
        if _found(word, title, lower):
            total -= 5
    for pattern in _PRICE_NUMBER:
        if pattern.search(title):
            total -= 4                      # 가격 수치가 제목에 박힌 기사
            break
    if _TRANSLATED.search(title.strip()):
        total -= 3
    return total


def topic(title: str) -> str:
    """주제 한 개로 분류한다.

    순서가 중요하다 — '활용 사례' 를 '산업 동향' 보다 먼저 본다. 엔비디아가
    모델을 공개한 기사는 산업 뉴스이기도 하지만, 사용자가 궁금한 것은
    '무엇을 만들었나' 쪽이므로 활용 사례로 묶는다.
    """
    lower = title.lower()
    ai = any(_found(w, title, lower) for w in _AI_WORDS)
    built = any(_found(w, title, lower) for w in _BUILT)
    coin = sum(1 for w in _COIN_WORDS if _found(w, title, lower))
    market = any(_found(w, title, lower) for w in _MARKET_WORDS)

    if ai and built:
        return TOPIC_USECASE
    if coin:
        return TOPIC_COIN
    if ai:
        return TOPIC_INDUSTRY
    if market:
        return TOPIC_MARKET
    if built:
        return TOPIC_USECASE
    return TOPIC_OTHER


_WORD = re.compile(r"[^\w가-힣]+")


def _tokens(title: str) -> set[str]:
    return {w for w in _WORD.sub(" ", title).split() if len(w) >= 2}


def _too_similar(tokens: set[str], seen: list[set[str]], threshold: float) -> bool:
    """같은 사건을 여러 매체가 쓴 기사를 걸러낸다.

    제목 해시(models.dedup_keys)는 글자가 하나라도 다르면 못 잡는다. 실측으로
    이 셋이 같은 전송에 들어왔다:
      · S&P500기업 분기이익 30% ↑…'엔비디아·마이크론' 쏠림
      · S&P500 이익 증가분 34%는 엔비디아·마이크론…'AI 쏠림' 심화
      · S&P500 분기이익 30% 급증 전망, 엔비디아·마이크론 쏠림
    겹치는 단어 비율로 보면 하나로 묶인다.
    """
    for other in seen:
        overlap = len(tokens & other) / max(1, min(len(tokens), len(other)))
        if overlap >= threshold:
            return True
    return False


def select(pending, cfg) -> tuple[list, list, dict]:
    """점수가 높은 것부터 상한만큼 고른다.

    pending 은 [(source, entry), ...]. 돌려주는 것은 (고른 것, 버린 것, 주제별 건수).
    고른 것은 주제 순서 → 점수 높은 순으로 정렬돼 있다.

    왜 소스 번갈아 뽑기(예전 _trim)를 버렸나: 모든 소스가 똑같이 기여하게 만들면
    좋은 기사가 없는 소스까지 한 자리를 차지한다. 실측에서 GeekNews 의 "Godot
    클라이언트 코어" 가 올라오고 경제 이슈의 더 좋은 기사가 밀렸다. 대신
    주제별 할당(quotas)과 소스당 상한(max_per_source)으로 쏠림을 막는다.

    ⚠️ 할당은 가중치 조정보다 확실하다. 실측에서 활용 사례 기사는 +3~+6,
       엔비디아 산업 기사는 +9~+18 이었다. 점수만으로 줄 세우면 활용 사례는
       한 건도 못 들어온다. 그래서 자리를 따로 보장한다.
    """
    cap = cfg.max_per_delivery
    quotas = dict(cfg.relevance_quotas)
    scored = []
    for source, entry in pending:
        value = score(entry.title, cfg.relevance_add, cfg.relevance_penalize)
        scored.append((value, topic(entry.title), source, entry))

    # 점수 높은 순. 같은 점수면 최신 글을 먼저 본다.
    scored.sort(key=lambda row: (-row[0], -(row[3].published.timestamp() if row[3].published else 0)))

    keep: list = []
    per_topic: dict[str, int] = {}
    per_source: dict[str, int] = {}
    seen_tokens: list[set[str]] = []

    def take(value, group, source, entry) -> None:
        entry.topic = group
        keep.append((value, group, source, entry))
        per_topic[group] = per_topic.get(group, 0) + 1
        per_source[source.key] = per_source.get(source.key, 0) + 1
        seen_tokens.append(_tokens(entry.title))

    for value, group, source, entry in scored:
        if cap and len(keep) >= cap:
            break
        if value < cfg.relevance_min_score:
            continue
        if per_topic.get(group, 0) >= quotas.get(group, cap or 0):
            continue
        if cfg.relevance_max_per_source and per_source.get(source.key, 0) >= cfg.relevance_max_per_source:
            continue
        if _too_similar(_tokens(entry.title), seen_tokens, cfg.relevance_similar):
            continue
        take(value, group, source, entry)

    # 할당·상한 때문에 자리가 남았다면 점수가 낮아도 채운다. 빈손보다 낫다.
    if cap and len(keep) < cap:
        chosen = {id(entry) for _, _, _, entry in keep}
        for value, group, source, entry in scored:
            if len(keep) >= cap:
                break
            if id(entry) in chosen or value < cfg.relevance_min_score:
                continue
            if _too_similar(_tokens(entry.title), seen_tokens, cfg.relevance_similar):
                continue
            take(value, group, source, entry)
            chosen.add(id(entry))

    keep.sort(key=lambda row: (TOPIC_ORDER.index(row[1]) if row[1] in TOPIC_ORDER else 99, -row[0]))
    counts: dict[str, int] = {}
    for _, group, _, _ in keep:
        counts[group] = counts.get(group, 0) + 1

    kept_ids = {id(entry) for _, _, _, entry in keep}
    dropped = [(source, entry) for source, entry in pending if id(entry) not in kept_ids]
    return [(source, entry) for _, _, source, entry in keep], dropped, counts
