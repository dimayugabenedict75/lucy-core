import re, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server"))
import stickers_tool as st

NAMES = ["Happy", "Excited", "Confident", "Silly", "Relieved", "Loving", "Grateful", "Shy", "Cozy", "Hopeful", "Sad", "Disappointed", "Lonely",
         "Tired", "Bored", "Angry", "Annoyed", "Shocked", "Panicked", "Jealous", "Confused", "Sneaky", "Scared", "Disgusted", "Smug"]

def make(extra=()):
    root = pathlib.Path(tempfile.mkdtemp())
    for theme in ("Dark", "Light"):
        d = root / "assets/stickers" / theme; d.mkdir(parents=True)
        for n in list(NAMES) + list(extra): (d / f"{n}.png").write_bytes(b"\x89PNG")
        (d / "Thumbs.db").write_bytes(b"x")
    (root / "assets/stickers/User").mkdir(); (root / "assets/stickers/User/mine.png").write_bytes(b"\x89PNG")
    return root

def test_themes_and_emotions_follow_the_files():
    root = make(extra=["Zany"]); lib = st.StickerLibrary(root, root.parent)
    assert lib.themes() == {"Dark": 26, "Light": 26}            # User/ never listed; Thumbs.db ignored
    assert lib.emotions("Dark")[:3] == ["Happy", "Excited", "Confident"] and lib.emotions("Light")[-1] == "Zany"
    assert len(st.DEFAULT_EMOTIONS) == 25 and st.DEFAULT_EMOTIONS == NAMES

def test_exact_case_and_theme_insensitive():
    lib = st.StickerLibrary(make())
    for emo in ("Smug", "smug", " SMUG ", "smug."):
        r = lib.resolve(emo, "dark"); assert r["ok"] and r["emotion"] == "Smug" and r["path"].name == "Smug.png" and r["theme"] == "Dark"
    r = lib.resolve("Cozy", "LIGHT"); assert r["ok"] and "Light" in r["path"].parts

def test_every_one_of_the_25_resolves_in_both_themes():
    lib = st.StickerLibrary(make())
    for theme in ("Dark", "Light"):
        for n in NAMES:
            r = lib.resolve(n, theme); assert r["ok"] and r["path"].is_file() and r["emotion"] == n, (theme, n)

def test_synonyms_and_phrases():
    lib = st.StickerLibrary(make())
    for word, want in {"glad": "Happy", "terrified": "Scared", "flirty": "Smug", "furious": "Angry", "thinking": "Confused",
                       "calm": "Cozy", "attuned": "Cozy", "exhausted": "Tired", "thankful": "Grateful", "gross": "Disgusted",
                       "very happy": "Happy", "so sad right now": "Sad", "envious": "Jealous", "hyped": "Excited",
                       "nervous": "Scared", "stressed": "Panicked", "sly": "Sneaky", "meh": "Bored", "Happpy": "Happy"}.items():
        r = lib.resolve(word, "Dark"); assert r["ok"] and r["emotion"] == want, (word, r)

def test_synonym_table_has_no_conflicts_and_only_real_emotions():
    seen = {}
    for emo, words in st.SYNONYMS.items():
        assert emo in st.DEFAULT_EMOTIONS, emo
        for w in words:
            assert w not in seen, f"'{w}' maps to both {seen[w]} and {emo}"
            seen[w] = emo
    assert not (set(map(st._norm, seen)) & {st._norm(e) for e in st.DEFAULT_EMOTIONS})  # a synonym never shadows a real name

def test_unknown_emotion_and_theme_give_helpful_errors():
    lib = st.StickerLibrary(make())
    r = lib.resolve("zzzxxxyy", "Dark"); assert not r["ok"] and "Valid emotions:" in r["error"] and "Smug" in r["error"]
    r = lib.resolve("Happy", "Neon"); assert not r["ok"] and "Available themes: Dark, Light" in r["error"]
    assert st.send_sticker({}, lib).startswith("Error: send_sticker needs an 'emotion'")

def test_media_marker_matches_send_file_convention():
    root = make(); home = root.parent
    out = st.send_sticker({"emotion": "Loving", "theme": "Dark", "caption": "for you"}, st.StickerLibrary(root, home))
    assert out == f"MEDIA:{root.name}/assets/stickers/Dark/Loving.png for you", out
    out = st.send_sticker({"emotion": "Loving"}, st.StickerLibrary(root, home)); assert out.endswith("Dark/Loving.png") and " " not in out
    out = st.send_sticker({"emotion": "Loving"}, st.StickerLibrary(root, pathlib.Path("/elsewhere"))); assert out.startswith("MEDIA:") and root.as_posix() in out and not out.startswith(f"MEDIA:{root.name}/")  # absolute fallback: full path, not rel

def test_missing_folder_falls_back_to_default_list():
    lib = st.StickerLibrary(pathlib.Path(tempfile.mkdtemp()))
    assert lib.emotions("Dark") == st.DEFAULT_EMOTIONS and not lib.resolve("Happy", "Dark")["ok"]

def test_new_sticker_files_are_picked_up_with_no_code_change():
    root = make(); lib = st.StickerLibrary(root, root.parent)
    assert "Mischievous" not in lib.all_emotions()
    (root / "assets/stickers/Dark/Mischievous.png").write_bytes(b"\x89PNG")            # only in Dark
    (root / "assets/stickers/Light/Dreamy.png").write_bytes(b"\x89PNG")             # only in Light
    assert lib.all_emotions()[-2:] == ["Dreamy", "Mischievous"] and lib.all_emotions()[:2] == ["Happy", "Excited"]
    assert st.send_sticker({"emotion": "mischievous"}, lib).endswith("Dark/Mischievous.png")
    assert st.send_sticker({"emotion": "Dreamy", "theme": "light"}, lib).endswith("Light/Dreamy.png")
    r = st.send_sticker({"emotion": "Dreamy", "theme": "Dark"}, lib)                    # exists in the other theme only
    assert r.startswith("Error: no sticker for 'Dreamy' in the Dark theme") and "Mischievous" in r
    (root / "assets/stickers/Dark/Mischievous.png").unlink(); assert "Mischievous" not in lib.all_emotions()  # removal too