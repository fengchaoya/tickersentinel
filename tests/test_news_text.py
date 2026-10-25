from datetime import timedelta

from tickersentinel.models import SECTOR, NewsItem
from tickersentinel.news.dedupe import dedupe
from tickersentinel.storage import Storage
from tickersentinel.textutil import canonical_url, normalize_title, strip_source_suffix, title_similarity

from conftest import utc

T = utc(2026, 10, 9, 14, 0)


def item(title, url, minutes=0, symbols=("MU",), source="Reuters"):
    return NewsItem(title=title, url=url, published_at=T + timedelta(minutes=minutes), source=source, symbols=symbols)


def test_canonical_url():
    a = canonical_url("https://www.reuters.com/tech/micron-falls/?utm_source=x&id=3#top")
    b = canonical_url("https://reuters.com/tech/micron-falls?id=3")
    assert a == b


def test_titles():
    assert normalize_title("Micron shares fall after guidance - Reuters") == "micron shares fall after guidance"
    assert strip_source_suffix("Micron jumps - CNBC", "CNBC") == "Micron jumps"
    assert title_similarity("Micron shares fall after weak guidance - Reuters",
                            "Micron shares fall after weak guidance | CNBC") > 0.9
    assert title_similarity("Micron shares fall after weak guidance", "Sandisk launches new SSD lineup") < 0.2
    assert title_similarity("SK海力士第三季度营业利润创新高", "SK海力士三季度营业利润创历史新高") > 0.5


def test_dedupe_keeps_filings_with_identical_titles():
    # 公告标题按模板生成：同一天的几份 Form 4、不同公司的业绩 8-K 标题几乎一样，但都是独立的公告
    def f(title, url, sym, minutes):
        return NewsItem(title, url, T + timedelta(minutes=minutes), source="SEC EDGAR", provider="sec_edgar",
                        symbols=(sym,), kind="filing")

    items = [
        f("MU 提交 4（内部人交易）", "https://www.sec.gov/Archives/edgar/data/723125/a/doc.xml", "MU", 0),
        f("MU 提交 4（内部人交易）", "https://www.sec.gov/Archives/edgar/data/723125/b/doc.xml", "MU", 2),
        f("MU 提交 8-K（重大事项公告）：业绩发布", "https://www.sec.gov/Archives/edgar/data/723125/c/8k.htm", "MU", 5),
        f("SNDK 提交 8-K（重大事项公告）：业绩发布", "https://www.sec.gov/Archives/edgar/data/2023554/d/8k.htm", "SNDK", 6),
        f("MU 提交 4（内部人交易）", "https://www.sec.gov/Archives/edgar/data/723125/a/doc.xml", "MU", 9),  # 同一 URL 仍去重
        NewsItem("MU 提交 4（内部人交易）", "https://n.example/repost", T + timedelta(minutes=20), symbols=("MU",)),
    ]
    out = dedupe(items)
    assert len(out) == 5 and sum(i.kind == "filing" for i in out) == 4
    assert all(len(i.symbols) == 1 for i in out)


def test_dedupe_keeps_earliest_and_merges_symbols():
    items = [
        item("Micron shares fall after weak guidance", "https://a.com/1", minutes=10, source="CNBC"),
        item("Micron shares fall after weak guidance - Reuters", "https://b.com/2", minutes=5, symbols=(SECTOR,)),
        item("Micron shares fall after weak guidance", "https://a.com/1?utm_source=tw", minutes=20),
        item("SK hynix starts HBM4 mass production", "https://c.com/3", minutes=30, symbols=("SKHY",)),
    ]
    out = dedupe(items)
    assert len(out) == 2
    assert out[0].title.startswith("SK hynix")  # 按时间倒序
    kept = out[1]
    assert kept.source == "Reuters" and set(kept.symbols) == {"MU", SECTOR}


def test_storage_seen_news_filter():
    st = Storage(":memory:")
    a, b = item("A news", "https://a.com/a"), item("B news", "https://a.com/b")
    assert st.unseen([a, b]) == [a, b]
    st.mark_seen([a], context="premarket")
    assert st.unseen([a, b]) == [b]
    # 同一文章带追踪参数也算已读
    assert st.unseen([item("A news", "https://www.a.com/a?utm_medium=x")]) == []
