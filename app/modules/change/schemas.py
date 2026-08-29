from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255, pattern=r'^[^\r\n<>]+$'),
]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
ChangeService = Literal['haproxy', 'nginx', 'apache', 'keepalived']
ChangeAction = Literal['save', 'reload', 'restart']
ChangeExecutionMode = Literal['rolling', 'parallel']
HealthCheckMode = Literal['full', 'config', 'service', 'none']
NotificationChannel = Literal['email', 'telegram', 'slack', 'mm', 'pd']
WebhookEvent = Literal[
    '*',
    'change.created',
    'change.updated',
    'change.validated',
    'change.validation_failed',
    'change.approved',
    'change.scheduled',
    'change.schedule_cancelled',
    'schedule.missed',
    'deployment.started',
    'deployment.batch_started',
    'deployment.batch_completed',
    'deployment.paused',
    'deployment.awaiting_promotion',
    'deployment.succeeded',
    'deployment.failed',
    'rollback.started',
    'rollback.succeeded',
    'rollback.failed',
    'drift.detected',
    'drift.resolved',
    'drift.check_failed',
]
ChangeStatus = Literal[
    'draft',
    'validating',
    'validated',
    'validation_failed',
    'pending_approval',
    'approved',
    'scheduled',
    'schedule_missed',
    'deploying',
    'pause_requested',
    'paused',
    'awaiting_promotion',
    'deployment_interrupted',
    'deployed',
    'auto_rolled_back',
    'auto_rollback_failed',
    'rolling_back',
    'rolled_back',
    'rollback_failed',
    'cancelled',
]


class NotificationDestination(BaseModel):
    channel: NotificationChannel
    recipient_id: int = Field(gt=0)


class ConfigChangeCreate(BaseModel):
    server_id: int | str
    service: ChangeService
    action: ChangeAction = 'reload'
    execution_mode: ChangeExecutionMode = 'rolling'
    batch_size: int | None = Field(default=None, ge=1, le=50)
    max_parallel: int = Field(default=8, ge=1, le=8)
    manual_promotion: bool = False
    health_check_mode: HealthCheckMode = 'full'
    health_check_retries: int = Field(default=1, ge=1, le=10)
    health_check_interval: int = Field(default=0, ge=0, le=60)
    canary_server_ids: list[int] = Field(default_factory=list, max_length=50)
    excluded_server_ids: list[int] = Field(default_factory=list, max_length=50)
    notification_channels: list[NotificationChannel] = Field(default_factory=list, max_length=5)
    notification_destinations: list[NotificationDestination] = Field(default_factory=list, max_length=50)
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
        if len(set(self.canary_server_ids)) != len(self.canary_server_ids):
            raise ValueError('canary_server_ids must not contain duplicates')
        if len(set(self.excluded_server_ids)) != len(self.excluded_server_ids):
            raise ValueError('excluded_server_ids must not contain duplicates')
        if set(self.canary_server_ids) & set(self.excluded_server_ids):
            raise ValueError('A rollout target cannot be both canary and excluded')
        if len(set(self.notification_channels)) != len(self.notification_channels):
            raise ValueError('notification_channels must not contain duplicates')
        destination_keys = [
            (destination.channel, destination.recipient_id)
            for destination in self.notification_destinations
        ]
        if len(set(destination_keys)) != len(destination_keys):
            raise ValueError('notification_destinations must not contain duplicates')
        if self.notification_destinations and self.notification_channels:
            destination_channels = {destination.channel for destination in self.notification_destinations}
            if destination_channels != set(self.notification_channels):
                raise ValueError('notification_channels must match notification_destinations')
        return self


class ConfigChangeUpdate(BaseModel):
    title: ShortText | None = None
    description: LongText | None = None
    action: ChangeAction | None = None
    execution_mode: ChangeExecutionMode | None = None
    batch_size: int | None = Field(default=None, ge=1, le=50)
    max_parallel: int | None = Field(default=None, ge=1, le=8)
    manual_promotion: bool | None = None
    health_check_mode: HealthCheckMode | None = None
    health_check_retries: int | None = Field(default=None, ge=1, le=10)
    health_check_interval: int | None = Field(default=None, ge=0, le=60)
    canary_server_ids: list[int] | None = Field(default=None, max_length=50)
    excluded_server_ids: list[int] | None = Field(default=None, max_length=50)
    notification_channels: list[NotificationChannel] | None = Field(default=None, max_length=5)
    notification_destinations: list[NotificationDestination] | None = Field(default=None, max_length=50)

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
        nullable_fields = (
            'max_parallel', 'manual_promotion', 'health_check_mode',
            'health_check_retries', 'health_check_interval',
            'canary_server_ids', 'excluded_server_ids',
            'notification_channels', 'notification_destinations',
        )
        for field_name in nullable_fields:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f'{field_name} must not be null')
        canaries = set(self.canary_server_ids or [])
        excluded = set(self.excluded_server_ids or [])
        if canaries & excluded:
            raise ValueError('A rollout target cannot be both canary and excluded')
        if self.notification_channels is not None and len(set(self.notification_channels)) != len(self.notification_channels):
            raise ValueError('notification_channels must not contain duplicates')
        if self.notification_destinations is not None:
            destination_keys = [
                (destination.channel, destination.recipient_id)
                for destination in self.notification_destinations
            ]
            if len(set(destination_keys)) != len(destination_keys):
                raise ValueError('notification_destinations must not contain duplicates')
        if self.notification_destinations and self.notification_channels:
            destination_channels = {destination.channel for destination in self.notification_destinations}
            if destination_channels != set(self.notification_channels):
                raise ValueError('notification_channels must match notification_destinations')
        return self


class ConfigChangeTargetUpdate(BaseModel):
    reason: LongText | None = None


class ConfigChangeQuery(BaseModel):
    service: ChangeService | None = None
    status: ChangeStatus | None = None


class ConfigChangeSchedule(BaseModel):
    scheduled_at: datetime
    maintenance_window_end: datetime | None = None

    @model_validator(mode='after')
    def validate_window(self):
        if self.maintenance_window_end and self.maintenance_window_end <= self.scheduled_at:
            raise ValueError('maintenance_window_end must be later than scheduled_at')
        return self


def _validate_webhook_url(value: str) -> str:
    value = str(value).strip()
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('url must be an absolute HTTP(S) URL')
    if parsed.username or parsed.password:
        raise ValueError('url must not contain embedded credentials')
    if parsed.fragment:
        raise ValueError('url must not contain a fragment')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('url contains an invalid port') from exc
    if port == 0:
        raise ValueError('url contains an invalid port')
    return value


class ConfigChangeWebhookCreate(BaseModel):
    name: ShortText
    url: str = Field(min_length=8, max_length=2048)
    secret: str | None = Field(default=None, max_length=4096)
    events: list[WebhookEvent] = Field(
        default_factory=lambda: ['deployment.succeeded', 'deployment.failed', 'drift.detected'],
        min_length=1,
        max_length=32,
    )
    enabled: bool = True
    verify_tls: bool = True

    _url_validator = field_validator('url')(_validate_webhook_url)

    @field_validator('events')
    @classmethod
    def unique_events(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('events must not contain duplicates')
        return value


class ConfigChangeWebhookUpdate(BaseModel):
    name: ShortText | None = None
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    secret: str | None = Field(default=None, max_length=4096)
    events: list[WebhookEvent] | None = Field(default=None, min_length=1, max_length=32)
    enabled: bool | None = None
    verify_tls: bool | None = None

    @field_validator('url')
    @classmethod
    def validate_optional_url(cls, value):
        return _validate_webhook_url(value) if value is not None else None

    @model_validator(mode='after')
    def validate_update(self):
        if not self.model_fields_set:
            raise ValueError('At least one field must be supplied')
        for name in ('name', 'url', 'events', 'enabled', 'verify_tls'):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f'{name} must not be null')
        if self.events is not None and len(set(self.events)) != len(self.events):
            raise ValueError('events must not contain duplicates')
        return self


class ConfigChangeAuditQuery(BaseModel):
    q: str | None = Field(default=None, max_length=255)
    service: ChangeService | None = None
    event_type: str | None = Field(default=None, max_length=64, pattern=r'^[a-z0-9_.-]+$')
    status: str | None = Field(default=None, max_length=64, pattern=r'^[a-z0-9_]+$')
    date_from: datetime | None = None
    date_to: datetime | None = None
    after_id: int | None = Field(default=None, ge=1)
    limit: int = Field(default=200, ge=1, le=500)

    @model_validator(mode='after')
    def validate_dates(self):
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError('date_to must not be earlier than date_from')
        return self


class ConfigChangeStatisticsQuery(BaseModel):
    days: int = Field(default=30, ge=1, le=365)


class ConfigChangeReportQuery(BaseModel):
    format: Literal['json', 'csv'] = 'json'
