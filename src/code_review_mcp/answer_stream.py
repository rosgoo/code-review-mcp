import re
from typing import NamedTuple

_THREAD_ID_KEY = re.compile(r'"thread_id"\s*:\s*"')
_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _Read(NamedTuple):
    text: str
    end: int
    closed: bool


def _hex4(raw: str, at: int) -> int | None:
    digits = raw[at : at + 4]
    if len(digits) < 4:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return None


def read_json_string(raw: str, start: int) -> _Read:
    """Decode the body of a JSON string from `start` (just after its opening quote).

    Stops at the closing quote, or before an escape that `raw` does not yet hold in full,
    so a later call from `end` continues where this one stopped. A surrogate pair is read
    as one character.
    """
    out: list[str] = []
    at = start
    while at < len(raw):
        char = raw[at]
        if char == '"':
            return _Read("".join(out), at + 1, True)
        if char != "\\":
            out.append(char)
            at += 1
            continue
        if at + 1 >= len(raw):
            break
        kind = raw[at + 1]
        if kind != "u":
            out.append(_ESCAPES.get(kind, kind))
            at += 2
            continue
        code = _hex4(raw, at + 2)
        if code is None:
            break
        if 0xD800 <= code < 0xDC00:
            if at + 8 > len(raw):
                break
            if raw[at + 6 : at + 8] == "\\u":
                low = _hex4(raw, at + 8)
                if low is None:
                    break
                if 0xDC00 <= low < 0xE000:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    at += 12
                    continue
        out.append(chr(code))
        at += 6
    return _Read("".join(out), at, False)


class AnswerStream:
    """Follows the streamed JSON input of one answer_question call.

    Each `feed` takes the next chunk of the input and returns the answer text that became
    readable. Until the `thread_id` value is complete it returns "", and the answer text
    read so far comes with the first chunk after it.
    """

    def __init__(self) -> None:
        self._raw = ""
        self._id_at: int | None = None
        self._answer_at: int | None = None
        self._parts: list[str] = []
        self._answer_closed = False
        self._emitted = 0
        self.thread_id: str | None = None

    @property
    def text(self) -> str:
        """The answer text read so far."""
        return "".join(self._parts)

    def feed(self, chunk: str) -> str:
        self._raw += chunk
        if self.thread_id is None:
            if self._id_at is None:
                key = _THREAD_ID_KEY.search(self._raw)
                self._id_at = key.end() if key else None
            if self._id_at is not None:
                read = read_json_string(self._raw, self._id_at)
                if read.closed:
                    self.thread_id = read.text
        if self._answer_at is None:
            key = _ANSWER_KEY.search(self._raw)
            self._answer_at = key.end() if key else None
        if self._answer_at is not None and not self._answer_closed:
            read = read_json_string(self._raw, self._answer_at)
            self._parts.append(read.text)
            self._answer_at = read.end
            self._answer_closed = read.closed
        if self.thread_id is None:
            return ""
        text = self.text
        new = text[self._emitted :]
        self._emitted = len(text)
        return new
