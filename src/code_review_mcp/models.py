"""Data models for the code review MCP server."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel


@dataclass
class Reply:
    id: str
    comment_id: str
    author: Literal["user", "claude"]
    message: str
    timestamp: str


@dataclass
class Comment:
    id: str
    file_path: str
    line_number: int
    line_type: Literal["add", "delete", "context"]
    line_content: str
    user_message: str
    timestamp: str
    status: Literal["draft", "submitted", "resolved"] = "draft"
    replies: list[Reply] = field(default_factory=list)


@dataclass
class FileView:
    path: str
    content: str
    language: str
    # For annotated file view: which lines are additions/deletions
    added_lines: list[int] = field(default_factory=list)
    deleted_lines: list[int] = field(default_factory=list)
    # Deleted line contents (keyed by the line number they appear before)
    deleted_content: dict[int, list[str]] = field(default_factory=dict)


@dataclass
class ReviewState:
    mode: Literal["diff", "files", "empty"] = "empty"
    diff_text: str = ""
    title: str = "Code Review"
    files: list[FileView] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    sse_subscribers: list[queue.SimpleQueue[str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Fires when the user clicks Submit Comments — wakes any blocked wait_for_comments call
    submit_event: threading.Event = field(default_factory=threading.Event)


class CommentRequest(BaseModel):
    file_path: str
    line_number: int
    line_type: Literal["add", "delete", "context"] = "context"
    line_content: str = ""
    user_message: str


class ReplyRequest(BaseModel):
    message: str
