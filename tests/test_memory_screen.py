"""Memory screen backends (edit / explain / vector status) and consolidation with Stella similarity."""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from lucy.memory import consolidate as C  # noqa: E402
from lucy.rag.conductor import Conductor  # noqa: E402
from test_embed import FakeNeural, mk  # noqa: E402


def two_facts(tmp_path, neural):
    c = mk(tmp_path, neural)
    c._bf_lock.acquire()                                  # no background backfill: the test controls vectors
    a = c.add_to_knowledge("dima drinks green tea", category="fact")["id"]
    b = c.add_to_knowledge("the gpu is an rx 9060 xt", category="fact")["id"]
    return c, a, b


def test_edit_changes_fields_clears_flag_and_stales_the_vector(tmp_path):
    n = FakeNeural({"tea": 0})
    c, a, _ = two_facts(tmp_path, n)
    c.backfill_vectors()
    assert c.vector_status()["mode"] == "neural"
    r = c.edit(a, text="dima drinks black tea now", category="preference", importance=5)
    assert r["status"] == "updated" and r["memory"]["category"] == "preference" and r["memory"]["importance"] == 5
    assert c.vector_status() == {"mode": "tfidf", "have": 1, "total": 2, "model": n.mid}        # edited text -> vector is stale
    assert c.edit(a, text="  ")["status"] == "error"
    assert c.edit(a, category="nonsense")["status"] == "error"
    assert c.edit(999, text="x")["status"] == "error"
    assert c.edit(a, importance=99)["memory"]["importance"] == 5                               # clamped 1-5


def test_explain_shows_mode_scores_and_what_goes_in_the_prompt(tmp_path):
    n = FakeNeural({"tea": 0, "gpu": 1})
    c, a, b = two_facts(tmp_path, n)
    c.add_to_knowledge("always answer briefly", category="instruction")
    c.backfill_vectors()
    r = c.explain("what tea does dima like")
    assert r["mode"] == "neural" and r["floor"] == c.MIN_COSINE
    tea = next(x for x in r["rows"] if x["id"] == a)
    assert tea["in_prompt"] and tea["above_floor"] and tea["score"] > 0.9
    pin = next(x for x in r["rows"] if x["text"].startswith("always"))
    assert pin["pinned"] and pin["in_prompt"]                                                   # pinned ones go in whatever the query is
    assert not next(x for x in r["rows"] if x["id"] == b)["in_prompt"]
    assert c.explain("   ")["rows"][0]["pinned"]                                                # blank query: still shows the pins


def test_explain_falls_back_to_keywords_and_says_so(tmp_path):
    c, a, _ = two_facts(tmp_path, FakeNeural(down=True))
    r = c.explain("green tea")
    assert r["mode"] == "tfidf" and "Keyword" in r["note"]


def test_consolidation_merges_paraphrases_tfidf_misses_when_stella_is_ready(tmp_path):
    n = FakeNeural({"tea": 0, "kitten": 1, "cat": 1})
    c = mk(tmp_path, n)
    c._bf_lock.acquire()
    a = c.add_to_knowledge("dima enjoys green tea in the morning")["id"]
    b = c.add_to_knowledge("my beverage of choice is tea")["id"]                                # same fact, no shared words
    c.backfill_vectors()
    sub = tmp_path / "plain"
    sub.mkdir()
    assert C.consolidate(mk(sub, None), dry_run=True)["mode"] == "tfidf"
    rep = C.consolidate(c)
    assert rep["mode"] == "neural+tfidf" and len(rep["merged"]) == 1
    assert len(c.all()) == 1 and len(c.archived()) == 1                                         # loser archived, never deleted


def test_consolidation_flags_a_paraphrased_conflict_instead_of_merging(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    c._bf_lock.acquire()
    c.add_to_knowledge("dima likes tea")
    c.add_to_knowledge("dima does not like tea")
    c.backfill_vectors()
    rep = C.consolidate(c)
    assert rep["merged"] == [] and len(rep["flagged"]) == 1 and len(c.all()) == 2


def test_consolidation_without_all_vectors_is_exactly_the_old_behaviour(tmp_path):
    n = FakeNeural({"tea": 0})
    c = mk(tmp_path, n)
    c._bf_lock.acquire()
    c.add_to_knowledge("dima likes tea")
    c.add_to_knowledge("my beverage of choice is tea")                                           # no vectors yet
    rep = C.consolidate(c)
    assert rep["mode"] == "tfidf" and rep["merged"] == []
