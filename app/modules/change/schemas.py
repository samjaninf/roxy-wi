from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints, model_validator
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255, pattern=r'^[^\r\n<>]+$'),
]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
ChangeService = Literal['haproxy', 'nginx', 'apache', 'keepalived']
ChangeAction = Literal['save', 'reload', 'restart']
ChangeExecutionMode = Literal['rolling', 'parallel']
ChangeStatus = Literal[
    'draft',
    'validating',
    'validated',
    'validation_failed',
    'pending_approval',
    'approved',
    'deploying',
    'deployment_interrupted',
    'deployed',
    'auto_rolled_back',
    'auto_rollback_failed',
    'rolling_back',
    'rolled_back',
    'rollback_failed',
    'cancelled',
]


class ConfigChangeCreate(BaseModel):
    server_id: int | str
    service: ChangeService
    action: ChangeAction = 'reload'
    execution_mode: ChangeExecutionMode = 'rolling'
    config: str
    file_path: str | None = None
    title: ShortText
    description: LongText | None = None
    requires_approval: bool = False

    @model_validator(mode='after')
    def require_remote_file_for_multi_config_services(self):
        if self.service in ('nginx', 'apache') and not self.file_path:
            raise ValueError(f'file_path is required for {self.service}')
        if not self.config.strip():
            raise ValueError('config must not be empty')
        return self


class ConfigChangeUpdate(BaseModel):
    title: ShortText | None = None
    description: LongText | None = None
    action: ChangeAction | None = None
    execution_mode: ChangeExecutionMode | None = None

    @model_validator(mode='after')
    def reject_empty_update(self):
        if not self.model_fields_set:
            raise ValueError('At least one field must be supplied')
        if 'title' in self.model_fields_set and self.title is None:
            raise ValueError('title must not be null')
        if 'action' in self.model_fields_set and self.action is None:
            raise ValueError('action must not be null')
        if 'execution_mode' in self.model_fields_set and self.execution_mode is None:
            raise ValueError('execution_mode must not be null')
        return self


class ConfigChangeQuery(BaseModel):
    service: ChangeService | None = None
    status: ChangeStatus | None = None
