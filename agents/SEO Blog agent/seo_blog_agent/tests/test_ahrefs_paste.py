from seo_blog_agent import ahrefs_paste


def test_metrics_from_overview_paste():
    text = "Keyword difficulty: 42\nVolume: 5.4K\nTraffic potential: 12,300"
    m = ahrefs_paste.parse_metrics(text)
    assert m == {"volume": 5400, "kd": 42, "traffic_potential": 12300}


def test_metrics_empty_and_garbage():
    assert ahrefs_paste.parse_metrics("") == {"volume": None, "kd": None, "traffic_potential": None}
    assert ahrefs_paste.parse_metrics("lorem ipsum")["volume"] is None


def test_competitor_csv_standard_export():
    text = (
        "Keyword,Current position,Volume,KD,Current URL\n"
        "legal virtual assistant,3,5400,42,https://x.com/post\n"
        "virtual paralegal services,7,880,21,https://x.com/post\n"
    )
    rows = ahrefs_paste.parse_competitor_csv(text)
    assert rows[0] == {"keyword": "legal virtual assistant", "volume": 5400,
                       "position": 3, "url": "https://x.com/post"}
    assert len(rows) == 2


def test_competitor_csv_skips_junk_and_reordered_headers():
    text = "junk line without commas\nVolume,Keyword\n1200,intake specialist\nnot,a,row,,\n"
    rows = ahrefs_paste.parse_competitor_csv(text)
    assert rows[0]["keyword"] == "intake specialist"
    assert rows[0]["volume"] == 1200


def test_dr_paste_variants():
    text = "clio.com 91\nwww.abajournal.com,88\nhttps://smokeball.com/blog: 74\nnot a line\nbaddr.com 999"
    dr = ahrefs_paste.parse_dr(text)
    assert dr == {"clio.com": 91, "abajournal.com": 88, "smokeball.com": 74}
