import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server"))
from tts_filter import TTSStreamFilter, clean_for_speech, scrub_paths

def stream(text, step=1):
    f = TTSStreamFilter(); out = ""
    for i in range(0, len(text), step): out += f.feed(text[i:i+step])
    return out + f.flush()

def spoken(text, step=1):
    return " ".join(clean_for_speech(stream(text, step)).split())

SCREENSHOT = (
    "You're right.\n\n"
    "I'm defaulting to the **TTS output** when I should just be delivering the text and letting the interface handle the audio.\n\n"
    "I've been treating the `send_tts` call as part of the *response*, rather than just a *side effect* of my thinking.\n\n"
    "I'll streamline the output. Less meta-commentary, more direct delivery.\n\n"
    '**Action:** `send_sticker(emotion="Attuned", theme="Dark")`\n'
    "MEDIA:C:/Users/dimay/Lucy/Lucy_Core/assets/stickers/Dark/Attuned.png")

def test_exact_screenshot_case_every_chunk_size():
    for step in (1, 2, 3, 7, 50, 10000):
        t = spoken(SCREENSHOT, step)
        assert "Action" not in t and "send_sticker" not in t and "Attuned" not in t and "MEDIA" not in t, (step, t)
        assert "C:" not in t and "Users" not in t and "png" not in t and "Dark" not in t, (step, t)
        assert t.startswith("You're right. I'm defaulting to the TTS output when I should just be delivering")
        assert t.endswith("Less meta-commentary, more direct delivery."), (step, t)
        assert "send tts call" in t                      # prose mentions of a tool name are still spoken

def test_action_variants_are_dropped_but_surrounding_speech_kept():
    cases = {
        "Sure thing.\nAction: send_file(file_path=\"C:/a/b.png\")\nDone!": "Sure thing. Done!",
        "On it. Action: web_search(query=\"x\") Then I answer.": "On it.",
        "Hello.\n  > **Action:** web_search\nAction Input: {\"query\": \"x\"}\nBye.": "Hello. Bye.",
        "Result.\nObservation: some tool output here\nAll good.": "Result. All good.",
        "I'll call send_tts(text=\"hi (there)\") now, okay?": "I'll call now, okay?",
    }
    for src, want in cases.items():
        for step in (1, 5, 999):
            assert spoken(src, step) == want, (src, step, spoken(src, step))

def test_paths_and_urls_removed_everywhere():
    for src, want in {
        "I saved it to C:\\Users\\dimay\\Desktop\\notes.txt. Next step.": "I saved it to. Next step.",
        "See https://example.com/a?b=1 for more.": "See for more.",
        "The file is at /mnt/c/Users/x/a.py and also ~/Lucy/b.md ok.": "The file is at and also ok.",
        "Look at assets/stickers/Dark/a.png now.": "Look at now.",
        "Read [the docs](https://x.io/doc) first.": "Read the docs first.",
        "Here ![sticker](C:/a/b.png) it is.": "Here it is.",
    }.items():
        for step in (1, 4, 999):
            assert spoken(src, step) == want, (src, step, spoken(src, step))

def test_normal_speech_is_untouched():
    for t in ["Hello there! How are you today?", "Use and or carefully, TCP IP is fine.", "The action: be kind.",
              "It costs 3.50 dollars, i.e. cheap. Version 3.2 is out.", "Let's meet at 5 p.m. tomorrow."]:
        got = spoken(t, 3)
        assert got.replace("(", "").replace("%", "") == t.replace("(", ""), (t, got)

def test_code_blocks_are_not_read():
    src = "Here is the fix.\n```python\nprint('hi')\nx = foo_bar(1)\n```\nThat should work."
    for step in (1, 6, 999): assert spoken(src, step) == "Here is the fix. That should work.", spoken(src, step)

def test_unterminated_things_never_swallow_the_reply():
    assert spoken("Text. send_x(unclosed forever") .startswith("Text.")
    f = TTSStreamFilter(); out = f.feed("Hello world, this is fine. ") + f.flush(); assert "fine." in out
    f = TTSStreamFilter(); out = f.feed("Action") + f.feed(" is needed here.") + f.flush(); assert out.strip() == "Action is needed here."

def test_helpers():
    assert clean_for_speech("Use and/or carefully, TCP/IP.") == "Use and or carefully, TCP IP."
    assert scrub_paths("see C:\\a\\b.txt.") == "see ." and clean_for_speech("**Bold** and send_tts") == "Bold and send tts"


NAMES = ("send_sticker", "remember", "forget", "web_search", "send_file", "recall_memory")

def stream_n(text, step=1, names=NAMES):
    f = TTSStreamFilter(names); out = ""
    for i in range(0, len(text), step): out += f.feed(text[i:i+step])
    return out + f.flush()

def spoken_n(text, step=1): return " ".join(clean_for_speech(stream_n(text, step), NAMES).split())

def test_brace_and_bracket_style_tool_calls_never_reach_the_voice():
    cases = {
        'Here you go. send_sticker{"emotion": "Happy", "theme": "Dark"} Hope that helps!': "Here you go. Hope that helps!",
        'Sure.\nsend_sticker {emotion: "Sad"}\nDone.': "Sure. Done.",
        'I will use remember(content="likes tea") now, okay?': "I will use now, okay?",
        'send_file[path="C:/a/b.png"] and then more words follow here.': "and then more words follow here.",
        'Look: {"emotion": "Happy"} that was JSON, and it is fine.': "Look: that was JSON, and it is fine.",
        'Okay. forget{"memory_id": 3} Anything else?': "Okay. Anything else?",
        "I can't say it doesn't matter, don't worry about it.": "I can't say it doesn't matter, don't worry about it.",
    }
    for src, want in cases.items():
        for step in (1, 2, 5, 999):
            assert spoken_n(src, step) == want, (src, step, spoken_n(src, step))

def test_clean_for_speech_alone_handles_calls_too():
    assert clean_for_speech('Here. send_sticker{"emotion": "Happy"} Bye.', NAMES) == "Here. Bye."
    assert clean_for_speech('x remember(content="a (b) c") y', NAMES) == "x y"
    assert clean_for_speech("The remember feature is handy.", NAMES) == "The remember feature is handy."   # name in prose is fine
