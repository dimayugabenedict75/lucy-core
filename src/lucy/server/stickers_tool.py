"""Lucy's own stickers (assets/stickers/<Theme>/<Emotion>.png) and the send_sticker tool.

The file name IS the emotion ("Happy.png", "Smug.png"), so there is no mapping to maintain: add a PNG to
both Dark/ and Light/ and it exists (restart Lucy Core to add it to the tool's emotion list). Themes are the
sub-folders of assets/stickers/ (Dark, Light); the folder User/ is the user's own picker and is never used here.

The tool returns "MEDIA:<path relative to the file-tool root> [caption]", exactly like send_file, so the chat
UI shows the image.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

# The agreed set (same in Dark and Light). Used for ordering and as a fallback if the folders are unreadable.
DEFAULT_EMOTIONS = [
    "Happy", "Excited", "Confident", "Silly", "Relieved", "Loving", "Grateful", "Shy", "Cozy", "Hopeful",
    "Sad", "Disappointed", "Lonely", "Tired", "Bored", "Angry", "Annoyed", "Shocked", "Panicked", "Jealous",
    "Confused", "Sneaky", "Scared", "Disgusted", "Smug",
]

# Words a model is likely to use -> the sticker that fits best. Each word belongs to ONE emotion.
SYNONYMS = {
    "Happy": ["glad", "joy", "joyful", "cheerful", "smile", "smiling", "pleased", "delighted", "content", "good", "upbeat", "positive", "bright"],
    "Excited": ["thrilled", "hyped", "enthusiastic", "eager", "pumped", "elated", "yay", "energetic", "ecstatic"],
    "Confident": ["assured", "bold", "proud", "determined", "capable", "ready", "resolute", "focused", "strong", "pride", "driven", "serious"],
    "Silly": ["playful", "goofy", "joking", "cheeky", "funny", "mischievous", "fun", "amused", "wacky", "lighthearted"],
    "Relieved": ["phew", "eased", "finally", "reassured", "weight off"],
    "Loving": ["love", "affection", "affectionate", "adore", "fond", "heart", "hearts", "caring", "tender", "warm", "sweet"],
    "Grateful": ["thankful", "thanks", "appreciative", "appreciation", "blessed", "thank you"],
    "Shy": ["bashful", "embarrassed", "blushing", "flustered", "timid", "awkward", "coy", "ashamed"],
    "Cozy": ["comfy", "comfortable", "snug", "peaceful", "calm", "relaxed", "cuddly", "serene", "drinking", "coffee", "tea", "sip", "attuned", "neutral"],
    "Hopeful": ["optimistic", "wishing", "anticipating", "hoping", "expectant", "looking forward"],
    "Sad": ["unhappy", "down", "sorrow", "hurt", "upset", "gloomy", "melancholy", "blue", "crying", "tears", "cry", "sob", "tearful", "sorry", "apologetic"],
    "Disappointed": ["let down", "dismayed", "bummed", "discouraged", "deflated", "underwhelmed"],
    "Lonely": ["alone", "isolated", "missing", "miss you", "solitary", "abandoned"],
    "Tired": ["sleepy", "exhausted", "yawn", "drowsy", "fatigued", "weary", "sleep", "bedtime"],
    "Bored": ["indifferent", "whatever", "meh", "uninterested", "dull", "apathetic", "unbothered", "dont care", "shrug"],
    "Angry": ["mad", "furious", "rage", "enraged", "livid", "hostile", "irate"],
    "Annoyed": ["irritated", "frustrated", "bothered", "grumpy", "irked", "fed up", "exasperated"],
    "Shocked": ["surprised", "stunned", "astonished", "amazed", "gasp", "speechless", "startled", "wow", "horrified"],
    "Panicked": ["panic", "freaking out", "alarmed", "frantic", "stressed", "overwhelmed", "hectic"],
    "Jealous": ["envious", "envy", "resentful", "possessive"],
    "Confused": ["puzzled", "baffled", "lost", "huh", "unsure", "perplexed", "uncertain", "thinking", "pondering", "curious", "thoughtful"],
    "Sneaky": ["sly", "scheming", "cunning", "plotting", "devious", "stealthy", "mischief"],
    "Scared": ["afraid", "frightened", "terrified", "fearful", "spooked", "fear", "nervous", "worried", "anxious", "uneasy", "tense"],
    "Disgusted": ["gross", "revolted", "yuck", "ew", "repulsed", "nauseated", "appalled", "eww"],
    "Smug": ["satisfied", "smirk", "smirking", "cocky", "triumphant", "superior", "flirty", "teasing", "wink", "winking", "charming", "pleased with myself"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower().replace("'", "").replace("’", "").replace("_", " ").replace("-", " ")).strip()


class StickerLibrary:
    def __init__(self, project_root: Path, safe_root: Path | None = None):
        self.dir = Path(project_root) / "assets" / "stickers"
        self.safe_root = Path(safe_root) if safe_root else None

    # -- what exists ------------------------------------------------------------------------
    def themes(self) -> dict[str, int]:
        """{theme: number of PNGs} for every folder except the user's own picker folder."""
        out: dict[str, int] = {}
        try:
            for d in sorted(self.dir.iterdir()):
                if d.is_dir() and d.name.lower() != "user":
                    out[d.name] = sum(1 for f in d.iterdir() if f.suffix.lower() == ".png")
        except OSError:
            pass
        return out

    def _theme_name(self, theme: str | None) -> str | None:
        want = (theme or "Dark").strip().lower()
        return next((name for name in self.themes() if name.lower() == want), None)

    def _files(self, theme_folder: str) -> dict[str, str]:
        """{emotion (file stem): file name} for one theme folder."""
        try:
            return {f.stem: f.name for f in (self.dir / theme_folder).iterdir()
                    if f.is_file() and f.suffix.lower() == ".png" and not f.name.startswith(".")}
        except OSError:
            return {}

    def emotions(self, theme: str = "Dark") -> list[str]:
        """Emotions available in a theme, in the agreed order (any extra stickers follow alphabetically)."""
        folder = self._theme_name(theme)
        stems = list(self._files(folder)) if folder else []
        if not stems:
            return list(DEFAULT_EMOTIONS)
        order = {e.lower(): i for i, e in enumerate(DEFAULT_EMOTIONS)}
        return sorted(stems, key=lambda s: (order.get(s.lower(), 999), s.lower()))

    def all_emotions(self) -> list[str]:
        """Every emotion present in any theme (agreed order first, extras alphabetically)."""
        seen: dict[str, str] = {}
        for theme in self.themes():
            for e in self.emotions(theme):
                seen.setdefault(e.lower(), e)
        if not seen:
            return list(DEFAULT_EMOTIONS)
        order = {e.lower(): i for i, e in enumerate(DEFAULT_EMOTIONS)}
        return sorted(seen.values(), key=lambda e: (order.get(e.lower(), 999), e.lower()))

    # -- lookup ------------------------------------------------------------------------------
    @staticmethod
    def _match(emotion: str, stems: list[str]) -> str | None:
        by_norm = {_norm(s): s for s in stems}
        q = _norm(emotion)
        if not q:
            return None
        if q in by_norm:
            return by_norm[q]
        for emo, words in SYNONYMS.items():
            if _norm(emo) in by_norm and q in map(_norm, words):
                return by_norm[_norm(emo)]
        padded = f" {q} "                                     # "very happy", "so sad right now"
        for key in sorted(by_norm, key=len, reverse=True):
            if f" {key} " in padded:
                return by_norm[key]
        for emo, words in SYNONYMS.items():
            if _norm(emo) in by_norm and any(f" {_norm(w)} " in padded for w in words):
                return by_norm[_norm(emo)]
        close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=0.72)
        return by_norm[close[0]] if close else None

    def resolve(self, emotion: str, theme: str | None = None) -> dict:
        """-> {"ok": True, "path", "emotion", "theme"} or {"ok": False, "error": "..."}"""
        folder = self._theme_name(theme)
        if folder is None:
            avail = ", ".join(self.themes()) or f"none found in {self.dir.as_posix()}"
            return {"ok": False, "error": f"Error: unknown sticker theme '{theme}'. Available themes: {avail}."}
        files = self._files(folder)
        stem = self._match(emotion, list(files))
        if stem is None:
            return {"ok": False, "error": f"Error: no sticker for '{emotion}' in the {folder} theme. "
                                          f"Valid emotions: {', '.join(self.emotions(folder))}."}
        return {"ok": True, "path": self.dir / folder / files[stem], "emotion": stem, "theme": folder}

    def media_path(self, path: Path) -> str:
        """The path as send_file reports it: relative to the file-tool root, forward slashes."""
        try:
            if self.safe_root:
                return path.resolve().relative_to(self.safe_root.resolve()).as_posix()
        except ValueError:
            pass
        return path.resolve().as_posix()

    def summary(self) -> str:
        parts = []
        for theme, n in self.themes().items():
            parts.append(f"{theme}: {n} stickers - {', '.join(self.emotions(theme))}")
        return "; ".join(parts)


def send_sticker(args: dict, lib: StickerLibrary) -> str:
    """Implementation of the send_sticker tool. Returns a MEDIA: line or an 'Error: ...' string."""
    emotion = str(args.get("emotion") or "").strip()
    if not emotion:
        return "Error: send_sticker needs an 'emotion'. Valid emotions: " + ", ".join(lib.emotions(args.get("theme") or "Dark")) + "."
    r = lib.resolve(emotion, args.get("theme"))
    if not r["ok"]:
        return r["error"]
    caption = str(args.get("caption") or "").strip()
    return f"MEDIA:{lib.media_path(r['path'])}" + (f" {caption}" if caption else "")
