"""Stops a model from repeating itself while it streams.

Small local models sometimes collapse into a loop and write the same sentence 10-20 times
("I'm patching agent_loop.py to redirect the output stream." x19). The text was already on screen
(and being spoken) before anything could react, so the guard works *while* the text streams:

  * text is cut into segments (a line, or a sentence ending in . ! ? followed by a space)
  * a segment that is still being written and *could* turn out to be a repeat of something already
    said is held back for a moment; the moment it diverges it is released, so normal text streams
    with practically no delay
  * a finished segment (>= MIN_LEN characters) that was already said this turn is dropped
  * after TRIP_AFTER repeats in a row the guard "trips": the caller cancels the request so the
    model stops generating, and retries with anti-repetition sampling

The memory spans the whole turn, so a nudge round that re-states the previous round's text
(another way to end up with the same paragraph three times) is cleaned up as well.
Code blocks are never touched (repeated lines are normal there).
"""

from __future__ import annotations

import re

_END = ".!?…。！？"
_NORM = re.compile(r"[\s*_`>#•\-–—]+")


def _normalize(seg: str) -> str:
    return _NORM.sub(" ", seg).strip().lower()


class RepeatGuard:
    MIN_LEN = 20          # shorter segments ("Done.", "Okay.") may legitimately repeat
    TRIP_AFTER = 3        # repeats in a row before the stream is cancelled
    RECENT = 40           # how many distinct recent segments are remembered

    def __init__(self, min_len: int | None = None, trip_after: int | None = None):
        self.min_len = min_len or self.MIN_LEN
        self.trip_after = trip_after or self.TRIP_AFTER
        self.recent: list[str] = []          # normalized, whole turn
        self.dups = 0                        # repeats in a row
        self.dropped = 0                     # repeats removed this turn
        self.tripped = False
        self.in_code = False
        self._seg = ""                       # current segment, everything so far
        self._pending = ""                   # part of it not yet released
        self._prev = ""                      # last character seen (for sentence ends)
        self._skip_blank = False             # swallow the blank line left behind by a dropped repeat
        self._round: list[str] = []          # text released this round
        self._seen_now: set[str] = set()     # segments finished in the current round

    # ----------------------------------------------------------------------------- public
    def begin_round(self) -> None:
        self._seg = self._pending = self._prev = ""
        self._round = []
        self._seen_now = set()
        self.tripped = False
        self.dups = 0

    def feed(self, text: str) -> str:
        """Text that is safe to show right now."""
        if self.tripped:
            return ""                                 # the caller is about to cancel the stream
        out = []
        for ch in text:
            self._seg += ch
            self._pending += ch
            if ch == "\n" or (ch in " \t" and self._prev in _END):
                out.append(self._close())
            elif not self.in_code and self._might_repeat():
                pass                                  # hold: could be a repeat in progress
            else:
                out.append(self._release())
            self._prev = ch
            if self.tripped:
                break
        return self._emit("".join(out))

    def flush(self) -> str:
        """End of the round: release (or drop, if it repeats) what is left."""
        if not self._seg:
            return ""
        return self._emit(self._close())

    def round_text(self) -> str:
        return "".join(self._round)

    # ----------------------------------------------------------------------------- internals
    def _emit(self, s: str) -> str:
        if s:
            self._round.append(s)
        return s

    def _release(self) -> str:
        s, self._pending = self._pending, ""
        return s

    def _might_repeat(self) -> bool:
        norm = _normalize(self._seg)
        if not norm:
            return False
        return any(r.startswith(norm) for r in self.recent if len(r) >= self.min_len)

    def _close(self) -> str:
        seg, pending = self._seg, self._pending
        self._seg = self._pending = ""
        if seg.count("```") % 2 == 1:                 # a code fence opens or closes here
            self.in_code = not self.in_code
            self.dups = 0
            return pending
        if self.in_code:
            return pending
        norm = _normalize(seg)
        if not norm:                                  # blank line
            return "" if self._skip_blank else pending
        if len(norm) >= self.min_len and norm in self.recent:
            self.dropped += 1
            self._skip_blank = True
            if norm in self._seen_now:                # repeating itself inside this very reply: that is the loop
                self.dups += 1
                if self.dups >= self.trip_after:
                    self.tripped = True
            self._seen_now.add(norm)                  # (a nudge round re-stating the previous round is only hidden)
            return ""
        if len(norm) >= self.min_len:
            self.dups = 0
            self._seen_now.add(norm)
            self.recent.append(norm)
            if len(self.recent) > self.RECENT:
                self.recent.pop(0)
        self._skip_blank = False
        return pending
