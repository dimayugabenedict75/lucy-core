import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server"))
from repeat_guard import RepeatGuard


def stream(text, g, n=3):
    out = ""
    for i in range(0, len(text), n):
        out += g.feed(text[i:i + n])
        if g.tripped:
            break
    return out + g.flush()


def test_loop_is_cut_after_the_first_copy():
    g = RepeatGuard(); g.begin_round()
    out = stream("I'm waiting for the **agent loop** to settle.\n\n" * 19, g)
    assert out.count("settle") == 1 and g.tripped and g.dropped == 3


def test_normal_text_passes_untouched_and_without_delay():
    g = RepeatGuard(); g.begin_round()
    text = "First sentence here that is long enough. Second one is different. Third is also unique and fairly long."
    assert stream(text, g) == text and not g.tripped
    g.begin_round()
    assert g.feed("A brand new sentence that starts differently") != ""          # released while streaming


def test_short_phrases_may_repeat():
    g = RepeatGuard(); g.begin_round()
    t = "Done. Done. Done. Done.\nYes.\nYes.\nYes.\n"
    assert stream(t, g) == t


def test_code_blocks_are_ignored():
    g = RepeatGuard(); g.begin_round()
    t = "```js\n" + "console.log('the same long line again');\n" * 5 + "```\n"
    assert stream(t, g) == t and g.dropped == 0


def test_memory_spans_rounds_but_only_same_round_repeats_trip():
    g = RepeatGuard(); g.begin_round()
    s = "I will check the configuration file now. Then I will report back to you. And finish the work."
    assert stream(s, g) == s
    g.begin_round()
    assert stream(s, g) == "" and g.dropped == 3 and not g.tripped               # hidden, but not a "loop"


def test_partial_repeat_is_held_then_released_when_it_diverges():
    g = RepeatGuard(); g.begin_round()
    g.feed("I am going to open the file and read it carefully. ")
    out = g.feed("I am going to open the door instead of that. ")
    out += g.flush()
    assert "door" in out


def test_tripped_guard_ignores_further_input():
    g = RepeatGuard(); g.begin_round()
    stream("Same long sentence repeated over and over again.\n" * 6, g)
    assert g.tripped and g.feed("more") == ""
