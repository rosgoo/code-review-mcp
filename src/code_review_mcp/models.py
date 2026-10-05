from typing import Literal

from pydantic import BaseModel

LineType = Literal["add", "delete", "context"]


class CommentRequest(BaseModel):
    file_path: str
    line_number: int
    line_type: LineType = "context"
    line_content: str = ""
    user_message: str


class ReplyRequest(BaseModel):
    message: str
