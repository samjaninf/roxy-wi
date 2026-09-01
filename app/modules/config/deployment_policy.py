"""Group-scoped controls for direct and Change Center configuration deployment."""

from typing import Mapping

from app.modules.db.db_model import Setting, connect
import app.modules.db.server as server_sql
from app.modules.roxywi.exception import RoxywiPermissionError, RoxywiValidationError


SERVICES = ('haproxy', 'nginx', 'apache', 'keepalived')
MODES = ('both', 'change_center', 'direct')
DEFAULT_MODE = 'both'
SETTING_SECTION = 'change_center'


def setting_name(service: str) -> str:
    if service not in SERVICES:
        raise RoxywiValidationError(f'Unsupported deployment policy service: {service}')
    return f'{service}_deployment_mode'


def get_mode(group_id: int, service: str) -> str:
    """Return one service policy, preserving legacy behaviour when it is absent."""
    param = setting_name(service)
    setting = Setting.get_or_none(
        (Setting.param == param) & (Setting.group_id == int(group_id))
    )
    if setting is None:
        return DEFAULT_MODE
    if setting.value not in MODES:
        raise RoxywiValidationError(
            f'Invalid deployment mode for {service}: {setting.value}'
        )
    return setting.value


def get_group_policy(group_id: int) -> dict[str, str]:
    params = {setting_name(service): service for service in SERVICES}
    settings = Setting.select(Setting.param, Setting.value).where(
        (Setting.group_id == int(group_id)) & (Setting.param.in_(tuple(params)))
    )
    policy = {service: DEFAULT_MODE for service in SERVICES}
    for setting in settings:
        service = params[setting.param]
        if setting.value not in MODES:
            raise RoxywiValidationError(
                f'Invalid deployment mode for {service}: {setting.value}'
            )
        policy[service] = setting.value
    return policy


def update_group_policy(group_id: int, modes: Mapping[str, str]) -> dict[str, str]:
    """Atomically upsert the complete deployment policy for a group."""
    normalized = {}
    for service in SERVICES:
        mode = modes.get(service)
        if mode not in MODES:
            raise RoxywiValidationError(
                f'Invalid deployment mode for {service}: {mode}'
            )
        normalized[service] = mode

    with connect().atomic():
        for service, mode in normalized.items():
            param = setting_name(service)
            updated = (
                Setting.update(value=mode, section=SETTING_SECTION)
                .where((Setting.param == param) & (Setting.group_id == int(group_id)))
                .execute()
            )
            if not updated:
                Setting.insert(
                    param=param,
                    value=mode,
                    section=SETTING_SECTION,
                    desc=f'Configuration deployment mode for {service}',
                    group_id=int(group_id),
                ).execute()
    return normalized


def direct_deployment_allowed(group_id: int, service: str) -> bool:
    return get_mode(group_id, service) in ('both', 'direct')


def change_center_creation_allowed(group_id: int, service: str) -> bool:
    return get_mode(group_id, service) in ('both', 'change_center')


def require_direct_deployment(
    group_id: int, service: str, *, action: str | None = None
) -> None:
    """Reject a direct remote write; validation-only requests remain available."""
    if action == 'test':
        return
    if not direct_deployment_allowed(group_id, service):
        raise RoxywiPermissionError(
            f'Direct {service.title()} configuration deployment is disabled for this group; '
            'create a change in Change Center'
        )


def require_direct_deployment_for_server(
    server_ip: str,
    service: str,
    *,
    action: str | None = None,
) -> None:
    server = server_sql.get_server_by_ip(server_ip)
    require_direct_deployment(server.group_id, service, action=action)


def require_change_center_creation(group_id: int, service: str) -> None:
    if not change_center_creation_allowed(group_id, service):
        raise RoxywiPermissionError(
            f'Change Center creation for {service.title()} is disabled for this group; '
            'use direct configuration deployment'
        )
