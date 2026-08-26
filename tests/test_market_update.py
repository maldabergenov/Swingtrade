"""Тесты маркет-апдейта: разбор лент, отбор новостей, деградация блоков, формат."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swingscan.marketupdate import report
from swingscan.marketupdate.app import build_update, deliver
from swingscan.marketupdate.config import (
    DEFAULT_CONFIG_PATH,
    FeedSpec,
    InstrumentSpec,
    MarketUpdateConfig,
    MarketUpdateConfigError,
)
from swingscan.marketupdate.feeds import parse_datetime, parse_feed, strip_html, within_window
from swingscan.marketupdate.models import (
    Headline,
    MarketSection,
    MarketUpdate,
    NewsSection,
    QuoteChange,
    SocialPost,
    SocialSection,
)
from swingscan.marketupdate.news import collect_news, rank_headlines
from swingscan.marketupdate.social import collect_social
from swingscan.marketupdate.summarize import enrich

NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ фикстуры
@pytest.fixture()
def config(tmp_path) -> MarketUpdateConfig:
    return MarketUpdateConfig(
        lookback_hours=16,
        max_headlines=5,
        max_tweets=3,
        indices=(InstrumentSpec("^GSPC", "S&P 500", "SPY"),),
        sectors=(
            InstrumentSpec("XLK", "Технологии"),
            InstrumentSpec("XLE", "Энергетика"),
            InstrumentSpec("XLF", "Финансы"),
        ),
        feeds=(FeedSpec("CNBC", "https://example.test/cnbc.xml"),),
        priority_high=("fed", "inflation"),
        priority_medium=("earnings",),
        news_use_alphavantage=False,
        state_dir=tmp_path / "state",
        reports_dir=tmp_path / "reports",
    )


def rss(items: str) -> str:
    return f"<?xml version='1.0'?><rss version='2.0'><channel>{items}</channel></rss>"


def item(title: str, url: str, published: datetime, description: str = "") -> str:
    stamp = published.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        f"<item><title>{title}</title><link>{url}</link>"
        f"<pubDate>{stamp}</pubDate><description>{description}</description></item>"
    )


class FakeResponse:
    def __init__(self, text: str = "", payload=None, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("нет JSON")
        return self._payload


class FakeSession:
    """Отдаёт заготовленный ответ по подстроке URL; иначе — сетевая ошибка."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None, params=None):
        self.calls.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise ConnectionError(f"нет маршрута для {url}")


# ------------------------------------------------------------------ конфиг
def test_repo_config_is_valid_and_has_editable_x_accounts():
    """Конфиг из репозитория должен читаться и содержать список аккаунтов."""
    loaded = MarketUpdateConfig.load(DEFAULT_CONFIG_PATH)
    assert loaded.indices and loaded.sectors and loaded.feeds
    assert "DeItaone" in loaded.x_accounts
    # По умолчанию доступа к X нет — блок обязан деградировать, а не падать.
    assert loaded.x_provider == "none"


def test_missing_config_file_is_reported_clearly(tmp_path):
    with pytest.raises(MarketUpdateConfigError, match="Не найден конфиг"):
        MarketUpdateConfig.load(tmp_path / "нет.toml")


def test_unknown_x_provider_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[x]\nprovider = "telepathy"\n', encoding="utf-8")
    with pytest.raises(MarketUpdateConfigError, match="provider"):
        MarketUpdateConfig.load(path)


def test_alphavantage_uses_etf_proxy_for_indices():
    spec = InstrumentSpec("^GSPC", "S&P 500", "SPY")
    assert spec.symbol_for("yfinance") == "^GSPC"
    assert spec.symbol_for("alphavantage") == "SPY"


# -------------------------------------------------------------------- ленты
def test_parse_feed_reads_rss_and_atom():
    parsed = parse_feed(rss(item("Fed holds rates", "https://a.test/1", NOW)))
    assert parsed[0].title == "Fed holds rates"
    assert parsed[0].published_at == NOW

    atom = (
        "<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'>"
        "<entry><title>CPI cools</title>"
        "<link rel='alternate' href='https://a.test/2'/>"
        "<published>2026-08-26T02:00:00Z</published></entry></feed>"
    )
    entries = parse_feed(atom)
    assert entries[0].title == "CPI cools"
    assert entries[0].url == "https://a.test/2"


def test_strip_html_and_parse_datetime_handle_real_feed_noise():
    assert strip_html("<p>Big   <b>news</b></p>") == "Big news"
    assert parse_datetime("2026-08-26T02:00:00Z").tzinfo is not None
    assert parse_datetime("не дата") is None
    assert parse_datetime(None) is None


def test_within_window_drops_stale_but_keeps_undated():
    items = parse_feed(
        rss(
            item("Свежая", "https://a.test/fresh", NOW - timedelta(hours=3))
            + item("Старая", "https://a.test/old", NOW - timedelta(hours=40))
            + "<item><title>Без даты</title><link>https://a.test/nd</link></item>"
        )
    )
    titles = {i.title for i in within_window(items, 16, now=NOW)}
    assert titles == {"Свежая", "Без даты"}


# ------------------------------------------------------------------ новости
def test_collect_news_ranks_macro_above_corporate(config):
    session = FakeSession(
        {
            "cnbc.xml": FakeResponse(
                rss(
                    item("Small cap earnings beat", "https://a.test/e", NOW - timedelta(hours=2))
                    + item("Fed signals rate cut", "https://a.test/f", NOW - timedelta(hours=2))
                )
            )
        }
    )
    section = collect_news(config, session=session, now=NOW)
    assert section.ok
    assert section.headlines[0].title == "Fed signals rate cut"
    assert section.headlines[0].tag == "ставки"


def test_collect_news_deduplicates_same_story_across_feeds(config):
    duplicated = [
        Headline("Fed holds rates steady", "https://a.test/1", "CNBC", NOW),
        Headline("The Fed holds rates steady", "https://b.test/2", "Reuters", NOW),
    ]
    assert len(rank_headlines(duplicated, config, now=NOW)) == 1


def test_collect_news_respects_per_source_quota(config):
    many = [
        Headline(f"Fed story number {n}", f"https://a.test/{n}", "CNBC", NOW)
        for n in range(6)
    ]
    # Квота на источник — 2, но при нехватке материала добор разрешён.
    assert len(rank_headlines(many, config, now=NOW)) == config.max_headlines


def test_collect_news_degrades_when_every_feed_is_down(config):
    session = FakeSession({"cnbc.xml": ConnectionError("таймаут")})
    section = collect_news(config, session=session, now=NOW)
    assert not section.ok
    assert "CNBC" in section.reason


def test_google_news_links_are_unwrapped(config):
    google = FeedSpec(
        "Reuters", "https://news.google.com/rss/search?q=site:reuters.com"
    )
    cfg = MarketUpdateConfig(**{**config.__dict__, "feeds": (google,)})
    wrapped = "https://news.google.com/rss/articles/x?url=https://reuters.com/real&amp;hl=en"
    session = FakeSession(
        {"news.google.com": FakeResponse(rss(item("Fed cuts", wrapped, NOW)))}
    )
    section = cfg and collect_news(cfg, session=session, now=NOW)
    assert section.headlines[0].url == "https://reuters.com/real"


# ------------------------------------------------------------------------ X
def test_social_is_marked_unavailable_without_access(config):
    section = collect_social(config, now=NOW)
    assert not section.ok
    assert section.hint  # подсказка, какой ключ добавить, уходит в отчёт


def test_social_x_api_parses_tweets(config):
    cfg = MarketUpdateConfig(
        **{**config.__dict__, "x_provider": "x_api", "x_bearer_token": "T", "x_accounts": ("DeItaone",)}
    )
    session = FakeSession(
        {
            "tweets/search/recent": FakeResponse(
                payload={
                    "data": [
                        {
                            "id": "1",
                            "author_id": "9",
                            "text": "BREAKING: CPI below forecast",
                            "created_at": "2026-08-26T01:00:00.000Z",
                            "public_metrics": {"like_count": 120},
                        }
                    ],
                    "includes": {"users": [{"id": "9", "username": "DeItaone"}]},
                }
            )
        }
    )
    section = collect_social(cfg, session=session, now=NOW)
    assert section.ok
    assert section.posts[0].author == "DeItaone"
    assert section.posts[0].url == "https://x.com/DeItaone/status/1"


def test_social_api_error_degrades_without_raising(config):
    cfg = MarketUpdateConfig(
        **{**config.__dict__, "x_provider": "x_api", "x_bearer_token": "T", "x_accounts": ("a",)}
    )
    session = FakeSession({"tweets/search/recent": FakeResponse(payload={"errors": ["403"]})})
    section = collect_social(cfg, session=session, now=NOW)
    assert not section.ok and "403" in section.reason


# -------------------------------------------------------------- отчёт целиком
def make_update(*, market_ok=True, news_ok=True, social_ok=False) -> MarketUpdate:
    market = (
        MarketSection(
            key="market",
            title="Итоги прошлой сессии",
            indices=[QuoteChange("^GSPC", "S&P 500", 5600.0, 0.82, 5554.4, "25.08.2026")],
            sectors=[
                QuoteChange("XLK", "Технологии", 250.0, 1.9, 245.3),
                QuoteChange("XLE", "Энергетика", 90.0, -1.2, 91.1),
                QuoteChange("XLF", "Финансы", 45.0, 0.3, 44.9),
            ],
            session_date="25.08.2026",
            provider="yfinance",
            context="S&P 500: умеренный рост.",
        )
        if market_ok
        else MarketSection.failed("market", "Итоги прошлой сессии", "источник лёг")
    )
    news = (
        NewsSection(
            key="news",
            title="Главные новости",
            headlines=[Headline("Fed signals cut", "https://a.test/1", "Reuters", NOW, tag="ставки")],
        )
        if news_ok
        else NewsSection.failed("news", "Главные новости", "ленты недоступны")
    )
    social = (
        SocialSection(
            key="social",
            title="Twitter / X",
            provider="x_api",
            posts=[SocialPost("DeItaone", "CPI below forecast", "https://x.com/a/1", NOW, 120)],
        )
        if social_ok
        else SocialSection.failed("social", "Twitter / X", "нет доступа", "добавьте X_BEARER_TOKEN")
    )
    return MarketUpdate(generated_at=NOW, market=market, news=news, social=social)


def test_report_marks_failed_section_instead_of_dropping_it(config):
    text = report.format_update(make_update(), config)
    assert "раздел временно недоступен" in text
    assert "добавьте X_BEARER_TOKEN" in text
    # Уцелевшие блоки на месте (амперсанд экранируется под HTML parse_mode).
    assert "S&amp;P 500" in text and "Fed signals cut" in text


def test_report_survives_all_three_sections_failing(config):
    update = make_update(market_ok=False, news_ok=False, social_ok=False)
    text = report.format_update(update, config)
    assert text.count("раздел временно недоступен") == 3
    assert update.all_failed


def test_report_fits_telegram_limit_after_split(config):
    from swingscan.report import TELEGRAM_LIMIT, split_message

    update = make_update(social_ok=True)
    update.news.headlines = [
        Headline(f"Fed story {n} " + "длинный заголовок " * 12, f"https://a.test/{n}", "Reuters", NOW)
        for n in range(40)
    ]
    chunks = split_message(report.format_update(update, config), TELEGRAM_LIMIT)
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)


def test_console_render_strips_html_and_links(config):
    text = report.format_console(make_update(), config)
    assert "<b>" not in text and "<a href" not in text
    assert "Reuters" in text


def test_save_update_writes_json_snapshot(config):
    path = report.save_update(make_update(), config)
    assert path.exists() and path.name.startswith("market_update_")


# ------------------------------------------------------------------ пересказ
class FakeLLM:
    """Минимальный двойник клиента Anthropic."""

    def __init__(self, text: str):
        self.messages = self
        self._text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        block = type("Block", (), {"type": "text", "text": self._text})()
        return type("Resp", (), {"content": [block], "stop_reason": "end_turn"})()


def test_enrich_applies_russian_retellings(config):
    update = make_update(social_ok=True)
    client = FakeLLM(
        '{"context": "Рынок вырос на ожиданиях смягчения.",'
        ' "headlines": [{"id": 0, "text": "ФРС намекнула на снижение ставки."}],'
        ' "posts": [{"id": 0, "text": "Инфляция вышла ниже прогноза."}]}'
    )
    enriched = enrich(update, config, client=client)
    assert enriched.llm_used
    assert enriched.news.headlines[0].retelling == "ФРС намекнула на снижение ставки."
    assert enriched.social.posts[0].retelling == "Инфляция вышла ниже прогноза."
    assert enriched.market.context.startswith("Рынок вырос")


def test_enrich_survives_broken_llm_output(config):
    update = make_update()
    enriched = enrich(update, config, client=FakeLLM("извините, не сегодня"))
    assert not enriched.llm_used
    assert enriched.news.headlines[0].retelling == ""
    assert any("пересказ" in w for w in enriched.warnings)


def test_enrich_without_api_key_is_a_warning_not_a_failure(config):
    update = make_update()
    enriched = enrich(update, config)  # ANTHROPIC_API_KEY не задан
    assert not enriched.llm_used
    assert any("ANTHROPIC_API_KEY" in w for w in enriched.warnings)


# ------------------------------------------------------------------ сквозной
def test_build_update_isolates_a_crashing_block(config, monkeypatch):
    monkeypatch.setattr(
        "swingscan.marketupdate.app.collect_market",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yfinance умер")),
    )
    session = FakeSession(
        {"cnbc.xml": FakeResponse(rss(item("Fed signals cut", "https://a.test/1", NOW)))}
    )
    update = build_update(config, session=session, now=NOW)
    assert not update.market.ok and "yfinance умер" in update.market.reason
    assert update.news.ok  # соседний блок не пострадал
    assert not update.all_failed


def test_deliver_sends_a_single_telegram_message(config):
    sent: list[str] = []

    class FakeTelegram:
        def send_message(self, text, chat_id=None):
            sent.append(text)
            return [{"message_id": 1}]

    assert deliver(make_update(), config, client=FakeTelegram()) is True
    assert "Маркет апдейт" in sent[0]


def test_deliver_dry_run_does_not_send(config):
    assert deliver(make_update(), config, dry_run=True) is False


def test_failed_feed_reason_stays_short_enough_for_telegram(config):
    """Трассировки requests не должны утекать в отчёт целиком."""
    proxy_error = ConnectionError(
        "HTTPSConnectionPool(host='search.cnbc.com', port=443): Max retries exceeded "
        "with url: /rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114 "
        "(Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel "
        "connection failed: 403 Forbidden')))"
    )
    session = FakeSession({"cnbc.xml": proxy_error})
    section = collect_news(config, session=session, now=NOW)
    assert not section.ok
    assert len(section.reason) < 120
    assert "ConnectionError" in section.reason


def test_many_dead_feeds_collapse_into_one_line(config):
    feeds = tuple(FeedSpec(f"Лента {n}", f"https://f{n}.test/rss") for n in range(8))
    cfg = MarketUpdateConfig(**{**config.__dict__, "feeds": feeds})
    session = FakeSession({".test/rss": ConnectionError("нет сети")})
    section = collect_news(cfg, session=session, now=NOW)
    assert "не ответило источников: 8" in section.reason
