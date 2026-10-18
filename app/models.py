from typing import Literal
from pydantic import BaseModel, Field, field_validator

class Channel(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = ''

class Batch(BaseModel):
    source: str
    destination: str
    folder: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9][A-Za-z0-9 _.-]*$')
    clip_seconds: int = Field(default=59, ge=1, le=60)
    framing: Literal['crop', 'fit'] = 'crop'
    start_date: str
    timezone: str = 'Asia/Kolkata'
    channels: list[Channel] = Field(default_factory=list, max_length=20)
    clips_per_day: int = Field(default=1, ge=1, le=24)
    window_start: str = Field(default='08:00')
    window_end: str = Field(default='20:00')
    title: str = Field(default='{video_name} — Part {part}', max_length=200)
    description: str = Field(default='', max_length=4500)
    hashtags: str = Field(default='#Shorts', max_length=400)
    visibility: Literal['private', 'unlisted', 'public'] = 'private'
    made_for_kids: Literal['yes', 'no'] = 'no'
    schedule: bool = False
    preview_token: str | None = None

    @field_validator('folder')
    @classmethod
    def folder_safe(cls, v):
        if v in ('.', '..') or v.endswith(('.', ' ')):
            raise ValueError('Choose a simple folder name')
        return v
