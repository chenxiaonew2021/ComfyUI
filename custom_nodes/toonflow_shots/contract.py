"""Versioned Toonflow → ComfyUI shot contract; no generation side effects."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Text = Annotated[str, Field(max_length=6000)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reference(Model):
    id: Identifier
    type: Literal["image", "video", "audio"]
    file: Annotated[str, Field(min_length=1, max_length=500)]
    storage: Literal["input", "output"] = "input"
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    purpose: Literal["identity", "wardrobe", "scene", "prop", "voice", "style", "composition", "continuity", "motion"]
    description: Text = ""


class Entity(Model):
    id: Identifier
    kind: Literal["character", "scene", "prop", "voice", "style"]
    version: Annotated[str, Field(min_length=1, max_length=100)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Text = ""
    wardrobe: Text = ""
    voice: Text = ""
    reference_ids: Annotated[list[Identifier], Field(min_length=1, max_length=12)]


class Bible(Model):
    version: Annotated[str, Field(min_length=1, max_length=100)]
    style: Annotated[str, Field(min_length=1, max_length=3000)]
    director_notes: Text = ""
    negative_prompt: Text = "不改变角色脸型、发型、服装和道具，不增加无关人物、字幕或水印。"
    entities: Annotated[list[Entity], Field(max_length=12)] = Field(default_factory=list)


class Camera(Model):
    shot_size: Text = ""
    angle: Text = ""
    movement: Text = ""
    lens: Text = ""


class Dialogue(Model):
    speaker_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    delivery: Text = ""
    kind: Literal["dialogue", "voiceover", "inner_voice"] = "dialogue"


class Shot(Model):
    description: Annotated[str, Field(min_length=1, max_length=6000)]
    entity_ids: list[Identifier] = Field(default_factory=list)
    camera: Camera = Field(default_factory=Camera)
    blocking: Text = ""
    dialogue: Annotated[list[Dialogue], Field(max_length=30)] = Field(default_factory=list)
    sound: Text = ""


class Continuity(Model):
    mode: Literal["independent", "match_previous"] = "independent"
    previous_shot_id: Identifier | None = None
    previous_last_frame_id: Identifier | None = None
    state_in: Text = ""
    state_out: Text = ""
    axis: Text = ""
    screen_direction: Text = ""
    lighting: Text = ""
    time_of_day: Text = ""


class Generation(Model):
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    workflow_version: Literal["short-drama-v1"] = "short-drama-v1"
    duration: Annotated[int, Field(ge=4, le=15)]
    ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"] = "16:9"
    resolution: Literal["768P"] = "768P"
    inference_steps: Annotated[int, Field(ge=1, le=2147483647)] = 5


class ShotRequest(Model):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier
    project_id: Identifier
    episode_id: Identifier
    shot_id: Identifier
    take: Annotated[int, Field(ge=1)] = 1
    bible: Bible
    shot: Shot
    continuity: Continuity = Field(default_factory=Continuity)
    references: Annotated[list[Reference], Field(min_length=1, max_length=12)]
    generation: Generation

    @model_validator(mode="after")
    def check_bindings(self):
        refs = {r.id: r for r in self.references}
        entities = {e.id: e for e in self.bible.entities}
        if len(refs) != len(self.references) or len(entities) != len(self.bible.entities):
            raise ValueError("素材 ID 和实体 ID 必须分别唯一")
        if len(set(self.shot.entity_ids)) != len(self.shot.entity_ids):
            raise ValueError("shot.entity_ids 不能重复")
        if set(self.shot.entity_ids) != set(entities):
            raise ValueError("bible.entities 只传本镜头使用的实体，并与 shot.entity_ids 一一对应")
        for kind, limit in (("image", 9), ("video", 3), ("audio", 3)):
            if sum(r.type == kind for r in self.references) > limit:
                raise ValueError(f"{kind} 参考数量超过 {limit}")
        if not any(r.type in ("image", "video") for r in self.references):
            raise ValueError("至少需要一张图片或一段视频参考")
        for entity in entities.values():
            if not set(entity.reference_ids) <= set(refs):
                raise ValueError(f"{entity.id} 引用了不存在的素材")
            kinds = {refs[r].type for r in entity.reference_ids}
            if entity.kind == "voice" and "audio" not in kinds:
                raise ValueError(f"声音实体 {entity.id} 需要音频参考")
            if entity.kind != "voice" and not kinds.intersection({"image", "video"}):
                raise ValueError(f"实体 {entity.id} 需要视觉参考")
        for line in self.shot.dialogue:
            if line.speaker_id not in entities or entities[line.speaker_id].kind not in ("character", "voice"):
                raise ValueError(f"台词说话人 {line.speaker_id} 必须绑定角色或声音实体")
        continuity = self.continuity
        if continuity.mode == "match_previous":
            ref = refs.get(continuity.previous_last_frame_id)
            if not continuity.previous_shot_id or not ref or ref.type != "image" or ref.purpose != "continuity":
                raise ValueError("接续上一镜必须指定上一镜 ID 和 purpose=continuity 的末帧图片")
            if not continuity.state_in.strip() or not continuity.state_out.strip():
                raise ValueError("接续镜头必须明确 state_in 和 state_out")
        elif continuity.previous_shot_id or continuity.previous_last_frame_id:
            raise ValueError("独立镜头不接受上一镜绑定；需要接续时使用 match_previous")
        return self
