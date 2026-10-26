"""기사를 '투자 관련성' 점수로 매겨 고르는 모듈.

왜 키워드 통과/탈락(filters.py)만으로는 부족한가 — 실측(2026-10-26, 386건 풀):

    경제 이슈  69/70  98.6% 통과
    기술 이슈  31/31   100% 통과
    AI 뉴스   100/100  100% 통과
    코인 뉴스  100/100  100% 통과

6개 소스 중 4개가 사실상 필터 없이 통과한다. 섹션 소스는 filter: exclude(소거법)
이라 include 를 안 보고, news 소스는 query 가 이미 걸렀다고 보아 include 를 건너뛴다
(config.SERVER_FILTERED). 그래서 321건이 통과하고 거기서 '최신 20건' 을 뽑았다.
결과는 "유럽 서머타임 종료", "대웅제약 헬리코박터 제균 연구", "실외기 추락사고" 가
섞여 오는 것이었다.

제외어를 늘리는 방향은 실측해 보고 버렸다. 후보 38개를 386건에 적용했을 때 각 1~2건만
걸렸고, '브리핑' 은 오히려 쓸만한 기사(SK하이닉스 실적, 코인 시세)를 지웠다.
'기부' 는 "찾기부터" 에 걸렸다 — 한글은 부분 문자열로 찾으므로 짧은 단어가 위험하다.

대신 '통과/탈락' 2단계를 '점수' 로 바꿨다. 같은 20건을 뽑되 **최신순이 아니라
점수순**으로 뽑는다. 넓게 받는 것(소거법)과 정밀하게 고르는 것은 서로 싸우지 않는다.
지금까지 싸운 이유는 통과한 것 중 '최신' 을 뽑았기 때문이다.

실측 결과 — 전송될 20건이 모두 투자 관련이 됐고, 같은 기사 중복(S&P500 기사 3개
매체)과 주제 쏠림(코인 7/20)도 사라졌다.

⚠️ 점수는 **제목만** 본다. news 소스의 summary 에는 매체명이 들어있어서
   (예: '코인리더스') 이를 함께 보면 매체명이 점수를 올려버린다. GeekNews 는
   summary 에 본문 설명이 들어있는데, 거기 'AI' 가 한 번 나오면 "Vim 8.x 포크",
   "맥 잠자기 방지 도구" 같은 글이 통과했다(실측 18건 중 대부분). 제목만 보면 5건.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_LATIN_ONLY = re.compile(r"[A-Za-z0-9]+")

# ── 점수를 올리는 신호 ────────────────────────────────────────────────
# 기업·종목명과 자산명은 가장 강한 신호다. 이것이 제목에 있으면 거의 틀림없이
# 내가 보고 싶은 기사다.
TICKER = """
엔비디아 삼성전자 SK하이닉스 하이닉스 마이크론 TSMC AMD 인텔 브로드컴 ASML 퀄컴
애플 테슬라 구글 메타 아마존 마이크로소프트 오픈AI OpenAI 앤트로픽 Anthropic 팔란티어
삼성전기 LG이노텍 LG에너지솔루션 삼성SDI SK온 네이버 카카오 현대차 기아 포스코 셀트리온
한미반도체 두산 한화 HD현대 조선 젠슨 리사 수 올트먼 머스크 파월 버핏
""".split()

ASSET = """
비트코인 이더리움 리플 XRP 솔라나 도지 알트코인 스테이블코인 가상자산 암호화폐 코인
코스피 코스닥 나스닥 다우 S&P500 S&P 닛케이 항셍 금값 유가 원유 환율 달러인덱스
""".split()

# 시장·수급 용어. 기업명 없이도 "무슨 일이 일어났는지" 를 말해주는 단어들.
MARKET = """
실적 영업이익 순이익 어닝 목표가 시총 시가총액 주가 자사주 배당 수주 공급계약 증설
감산 증산 상장 공모주 ETF 청산 급등 급락 폭락 폭등 매수 매도 순매수 외국인 기관 공매도
밸류에이션 거래대금 지배력
""".split()

MACRO = """
금리 기준금리 연준 FOMC 관세 국채 물가 소비자물가 인플레이션 무역 수출 공급망 제재
GDP 실업률 고용지표 경기침체 리세션 가계대출 외환 경상수지
""".split()

TECH = """
반도체 HBM GPU 파운드리 D램 낸드 데이터센터 LLM 온디바이스 휴머노이드 로봇 자율주행
배터리 전기차 양자 AI반도체 가속기
""".split()

# ── 점수를 내리는 신호 ────────────────────────────────────────────────
# 주제어가 아니라 '기사 유형' 이다. 그래서 오탐이 적다. 보도자료·행사·인사 기사는
# 섹션 피드에 끊임없이 섞여 들어오는데 투자 판단에는 쓸모가 없다.
PRESS = """
개최 세미나 컨퍼런스 총회 박람회 엑스포 공모전 경진대회 캠프 워크숍 간담회 포럼 시상
수상 위촉 임명 선임 인사 출범 개소 발대 협약 MOU 기념 이벤트 체험 공고 모집 접수
리뉴얼 캠페인 역량 실증 착수
""".split()

# 관공서·지자체·대학의 AI 사업 기사. 'AI 뉴스' 검색에서 100건 중 26건이 이것이었다
# (인천시 AI 챔피언 컨설팅, LH AI 프론티어, 서산교육지원청, 종로구의원 조례 발의…).
GOV = """
교육청 지원청 구의원 시의원 도의원 군수 진흥원 공단 항만공사 도시공사 대학교 학과
조례 행정업무 장학 봉사 민원 지자체 특별시 교육지원
""".split()

# 의료·제약 연구 기사. 구글 '경제' 섹션에 제약사 보도자료가 대량으로 들어온다
# (대웅제약 헬리코박터 제균 연구, 대한종양내과학회 환자 1000명 등록 등 실측 6건).
MED = """
임상 제균 항암 환자 학술 논문 게재 교수 병원 의료기기 치과 임플란트 신약 처방 약가
비만 치료제 백신 진단
""".split()

# 생활·부동산·사건사고. '경제' 섹션의 절반이 이쪽이다.
LIFE = """
집값 전셋값 아파트 재건축 분양 오피스텔 전세 임대 보이스피싱 추락 음료 맛집 여행
운세 부고 서머타임 연금 노인 알바 월급 쿠폰 상품권 이어폰 폴더블 내구성
개인정보 해킹 취약점 유출
""".split()

# (단어목록, 가중치). 한 기사에 여러 개가 걸리면 모두 더한다.
SIGNALS: list[tuple[list[str], int]] = [
    (TICKER, 4), (ASSET, 4), (MARKET, 2), (MACRO, 2), (TECH, 2),
    (PRESS, -4), (GOV, -5), (MED, -5), (LIFE, -4),
]

# 자동번역 기사는 제목이 평서문으로 끝난다. 실측된 것들:
#   "동남아시아에서 인공지능(AI) 수요가 급증하고 있습니다."
#   "인공지능의 오용은 학생들의 독립적인 사고력을 저해할 수 있다."
# 한국 기사 제목은 이렇게 끝나지 않는다(명사·따옴표·물음표로 끝난다).
_TRANSLATED = re.compile(r"(습니다|됩니다|입니다|있다|이다)\.?\s*$")

# ── 주제 분류 ─────────────────────────────────────────────────────────
# 점수 할당(한 주제가 상한을 독차지하지 않게)과 메시지 묶기에 같은 분류를 쓴다.
TOPIC_COIN = "코인"
TOPIC_AI = "AI·반도체"
TOPIC_MARKET = "주식·거시"
TOPIC_OTHER = "기타"
TOPIC_ORDER = [TOPIC_AI, TOPIC_MARKET, TOPIC_COIN, TOPIC_OTHER]

_COIN_WORDS = set("""
비트코인 이더리움 리플 XRP 솔라나 도지 알트코인 스테이블코인 가상자산 암호화폐 코인
업비트 빗썸 바이낸스
""".split())

_AI_WORDS = set("""
엔비디아 반도체 HBM GPU 파운드리 D램 낸드 데이터센터 LLM 온디바이스 AI반도체 가속기
오픈AI OpenAI 앤트로픽 Anthropic 삼성전자 SK하이닉스 하이닉스 마이크론 TSMC AMD 인텔
브로드컴 ASML 삼성전기 LG이노텍 휴머노이드 로봇 자율주행
퀄컴 엑시노스 스냅드래곤 메모리 AP 칩
""".split())

_MARKET_WORDS = set("""
코스피 코스닥 나스닥 다우 S&P500 S&P 닛케이 금리 기준금리 연준 FOMC 관세 국채 물가
환율 유가 원유 무역 수출 실적 영업이익 목표가 시총 주가 자사주 배당 수주 공모주
공매도 외국인 급등 급락
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


def score(title: str, bonus: list[str] = (), penalty: list[str] = ()) -> int:
    """제목 하나의 투자 관련성 점수. 높을수록 먼저 보낸다."""
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
    if _TRANSLATED.search(title.strip()):
        total -= 3
    return total


def topic(title: str) -> str:
    """주제 한 개로 분류한다. 코인·AI 가 함께 나오면 많이 걸린 쪽을 택한다."""
    lower = title.lower()
    coin = sum(1 for w in _COIN_WORDS if _found(w, title, lower))
    ai = sum(1 for w in _AI_WORDS if _found(w, title, lower))
    market = sum(1 for w in _MARKET_WORDS if _found(w, title, lower))
    if coin and coin >= ai:
        return TOPIC_COIN
    if ai:
        return TOPIC_AI
    if market:
        return TOPIC_MARKET
    return TOPIC_OTHER


_WORD = re.compile(r"[^\w가-힣]+")


def _tokens(title: str) -> set[str]:
    return {w for w in _WORD.sub(" ", title).split() if len(w) >= 2}


def _too_similar(tokens: set[str], seen: list[set[str]], threshold: float) -> bool:
    """같은 사건을 여러 매체가 쓴 기사를 걸러낸다.

    제목 해시(models.dedup_keys)는 글자가 하나라도 다르면 못 잡는다. 실측으로
    이런 세 건이 같은 전송에 들어왔다:
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


def select(pending, cfg) -> tuple[list, int, dict]:
    """점수가 높은 것부터 상한만큼 고른다.

    pending 은 [(source, entry), ...]. 돌려주는 것은 (고른 것, 버린 수, 주제별 건수).
    고른 것은 주제 순서 → 점수 높은 순으로 정렬돼 있다 (메시지에 그 순서로 나간다).

    왜 소스 번갈아 뽑기(예전 _trim)를 버렸나: 모든 소스가 똑같이 기여하게 만들면
    좋은 기사가 없는 소스까지 한 자리를 차지한다. 실측에서 GeekNews 의 "Godot
    클라이언트 코어"(+4)가 올라오고 경제 이슈의 더 좋은 기사가 밀렸다. 대신
    주제별 할당(quotas)과 소스당 상한(max_per_source)으로 쏠림을 막는다.
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
    for value, group, source, entry in scored:
        if cap and len(keep) >= cap:
            break
        if value < cfg.relevance_min_score:
            continue
        if per_topic.get(group, 0) >= quotas.get(group, cap or 0):
            continue
        if cfg.relevance_max_per_source and per_source.get(source.key, 0) >= cfg.relevance_max_per_source:
            continue
        tokens = _tokens(entry.title)
        if _too_similar(tokens, seen_tokens, cfg.relevance_similar):
            continue
        entry.topic = group
        keep.append((value, group, source, entry))
        per_topic[group] = per_topic.get(group, 0) + 1
        per_source[source.key] = per_source.get(source.key, 0) + 1
        seen_tokens.append(tokens)

    # 할당·상한 때문에 자리가 남았다면 점수가 낮아도 채운다. 빈손으로 보내는 것보다 낫다.
    if cap and len(keep) < cap:
        chosen = {id(entry) for _, _, _, entry in keep}
        for value, group, source, entry in scored:
            if len(keep) >= cap:
                break
            if id(entry) in chosen or value < cfg.relevance_min_score:
                continue
            tokens = _tokens(entry.title)
            if _too_similar(tokens, seen_tokens, cfg.relevance_similar):
                continue
            entry.topic = group
            keep.append((value, group, source, entry))
            seen_tokens.append(tokens)
            chosen.add(id(entry))

    keep.sort(key=lambda row: (TOPIC_ORDER.index(row[1]) if row[1] in TOPIC_ORDER else 99, -row[0]))
    counts: dict[str, int] = {}
    for _, group, _, _ in keep:
        counts[group] = counts.get(group, 0) + 1

    kept_ids = {id(entry) for _, _, _, entry in keep}
    dropped = [(source, entry) for source, entry in pending if id(entry) not in kept_ids]
    return [(source, entry) for _, _, source, entry in keep], dropped, counts
