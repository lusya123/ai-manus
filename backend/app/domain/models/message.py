from typing import List
from pydantic import BaseModel, Field

class Message(BaseModel):
    message: str = ""
    attachments: List[str] = Field(default_factory=list)
