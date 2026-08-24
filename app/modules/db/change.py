from datetime import datetime
from time import sleep

from peewee import OperationalError

from app.modules.db.common import out_error
from app.modules.db.db_model import ConfigChange, ConfigChangeTarget
from app.modules.roxywi.exception import RoxywiConflictError, RoxywiResourceNotFound


_UPDATABLE_FIELDS = {
    'action', 'approved_by', 'deployment_output', 'deployed_at', 'description', 'diff',
    'execution_mode', 'rollback_output', 'status', 'title', 'updated_at', 'validation_output',
}

_TARGET_UPDATABLE_FIELDS = {
    'deployed_at', 'deployment_output', 'rollback_output', 'status', 'updated_at',
    'validation_output',
}

_LOCK_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0)


def _is_database_locked(exc: Exception) -> bool:
    return isinstance(exc, OperationalError) and 'database is locked' in str(exc).lower()


def _execute_write(operation):
    """Retry short Change Center writes when another SQLite writer is finishing."""
    for attempt in range(len(_LOCK_RETRY_DELAYS) + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_database_locked(exc):
                raise
            if attempt == len(_LOCK_RETRY_DELAYS):
                raise RoxywiConflictError(
                    'The database is busy with another Roxy-WI task. Wait a moment and retry.'
                ) from exc
            sleep(_LOCK_RETRY_DELAYS[attempt])


def create_change(**values) -> ConfigChange:
    try:
        return _execute_write(lambda: ConfigChange.create(**values))
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)


def get_change(change_id: int) -> ConfigChange:
    try:
        return ConfigChange.get(ConfigChange.id == change_id)
    except ConfigChange.DoesNotExist as exc:
        raise RoxywiResourceNotFound from exc
    except Exception as exc:
        return out_error(exc)


def list_changes(group_id: int, *, service: str | None = None, status: str | None = None):
    query = ConfigChange.select().where(ConfigChange.group_id == group_id)
    if service:
        query = query.where(ConfigChange.service == service)
    if status:
        query = query.where(ConfigChange.status == status)
    try:
        return query.order_by(ConfigChange.created_at.desc()).execute()
    except Exception as exc:
        return out_error(exc)


def update_change(change_id: int, **values) -> ConfigChange:
    invalid_fields = set(values) - _UPDATABLE_FIELDS
    if invalid_fields:
        raise ValueError(f'Unsupported change fields: {", ".join(sorted(invalid_fields))}')
    values['updated_at'] = datetime.now()
    try:
        _execute_write(
            lambda: ConfigChange.update(**values).where(ConfigChange.id == change_id).execute()
        )
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)
    return get_change(change_id)


def transition_change(change_id: int, from_statuses: tuple[str, ...], status: str, **values) -> ConfigChange:
    """Atomically claim a workflow transition and reject concurrent actions."""
    invalid_fields = set(values) - _UPDATABLE_FIELDS
    if invalid_fields:
        raise ValueError(f'Unsupported change fields: {", ".join(sorted(invalid_fields))}')
    values.update(status=status, updated_at=datetime.now())
    try:
        updated = _execute_write(
            lambda: (
                ConfigChange.update(**values)
                .where((ConfigChange.id == change_id) & (ConfigChange.status.in_(from_statuses)))
                .execute()
            )
        )
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)
    if updated != 1:
        raise RoxywiConflictError('The change is no longer in a state that allows this operation')
    return get_change(change_id)


def create_targets(change_id: int, targets: list[dict]) -> list[ConfigChangeTarget]:
    """Persist an immutable rollout topology for a configuration change."""
    if not targets:
        return []
    values = [dict(target, change=change_id) for target in targets]
    database = ConfigChangeTarget._meta.database
    try:
        def insert_targets():
            with database.atomic():
                return ConfigChangeTarget.insert_many(values).execute()

        _execute_write(insert_targets)
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)
    return list_targets(change_id)


def list_targets(change_id: int) -> list[ConfigChangeTarget]:
    try:
        return list(
            ConfigChangeTarget.select()
            .where(ConfigChangeTarget.change == change_id)
            .order_by(ConfigChangeTarget.position.asc())
        )
    except Exception as exc:
        return out_error(exc)


def get_target(target_id: int) -> ConfigChangeTarget:
    try:
        return ConfigChangeTarget.get(ConfigChangeTarget.id == target_id)
    except ConfigChangeTarget.DoesNotExist as exc:
        raise RoxywiResourceNotFound from exc
    except Exception as exc:
        return out_error(exc)


def update_target(target_id: int, **values) -> ConfigChangeTarget:
    invalid_fields = set(values) - _TARGET_UPDATABLE_FIELDS
    if invalid_fields:
        raise ValueError(f'Unsupported target fields: {", ".join(sorted(invalid_fields))}')
    updated_at = datetime.now()
    values['updated_at'] = updated_at
    target = get_target(target_id)
    database = ConfigChangeTarget._meta.database
    try:
        def update_with_heartbeat():
            with database.atomic():
                ConfigChangeTarget.update(**values).where(
                    ConfigChangeTarget.id == target_id
                ).execute()
                ConfigChange.update(updated_at=updated_at).where(
                    ConfigChange.id == target.change_id
                ).execute()

        _execute_write(update_with_heartbeat)
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)
    return get_target(target_id)


def reset_targets(change_id: int, *, status: str = 'pending') -> list[ConfigChangeTarget]:
    updated_at = datetime.now()
    database = ConfigChangeTarget._meta.database
    try:
        def reset_with_heartbeat():
            with database.atomic():
                (
                    ConfigChangeTarget.update(
                        status=status,
                        deployment_output=None,
                        rollback_output=None,
                        deployed_at=None,
                        updated_at=updated_at,
                    )
                    .where(ConfigChangeTarget.change == change_id)
                    .execute()
                )
                ConfigChange.update(updated_at=updated_at).where(
                    ConfigChange.id == change_id
                ).execute()

        _execute_write(reset_with_heartbeat)
    except RoxywiConflictError:
        raise
    except Exception as exc:
        return out_error(exc)
    return list_targets(change_id)
