from typing import Literal

from pydantic import BaseModel, Field

from code_review_mcp.store import Side

LineType = Literal["add", "delete", "context"]


class CommentRequest(BaseModel):
    path: str
    side: Side
    line: int = Field(ge=0)
    start_line: int | None = Field(default=None, ge=1)
    start_side: Side | None = None
    line_content: str = ""
    body: str


class ReplyRequest(BaseModel):
    message: str
