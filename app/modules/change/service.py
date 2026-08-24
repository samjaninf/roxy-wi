import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from shlex import quote
from uuid import uuid4

import app.modules.common.common as common
import app.modules.config.common as config_common
import app.modules.config.config as config_mod
import app.modules.db.change as change_sql
import app.modules.db.config as config_sql
import app.modules.db.server as server_sql
import app.modules.db.service as service_sql
import app.modules.db.sql as sql
import app.modules.db.user as user_sql
import app.modules.server.server as server_mod
import app.modules.service.common as service_common
from app.modules.subscription.access import CHANGE_CENTER, require_feature
from app.modules.roxywi.exception import (
    RoxywiConflictError,
    RoxywiPermissionError,
    RoxywiValidationError,
)


EDITABLE_STATUSES = ('draft', 'validation_failed')
VALIDATABLE_STATUSES = ('draft', 'validation_failed')
CANCELLABLE_STATUSES = ('draft', 'validation_failed', 'validated', 'pending_approval', 'approved')
RETRYABLE_DEPLOY_STATUSES = (
    'auto_rolled_back', 'auto_rollback_failed', 'rollback_failed', 'deployment_interrupted'
)
IN_PROGRESS_STATUSES = ('validating', 'deploying', 'rolling_back')
STALE_OPERATION_TIMEOUT = timedelta(minutes=5)
ERROR_PATTERN = re.compile(r'(^|[\n>])\s*(?:[^\n:]+:\s*)?error:', re.IGNORECASE)
ANSI_PATTERN = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class _RolloutFailure(RuntimeError):
    def __init__(self, message: str, affected_target_ids: list[int]):
        super().__init__(message)
        self.affected_target_ids = affected_target_ids


def _safe_name(value: str, fallback: str) -> str:
    value = re.sub(r'[^A-Za-z0-9_.-]+', '-', value).strip('-')
    return value or fallback


def _change_paths(service: str, server_ip: str) -> tuple[Path, Path]:
    changes_dir = Path(config_common.get_config_dir(service)).resolve() / 'changes'
    changes_dir.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    extension = config_common.get_file_format(service)
    server_name = _safe_name(server_ip, 'server')
    return (
        changes_dir / f'{server_name}-{token}-draft.{extension}',
        changes_dir / f'{server_name}-{token}-before.{extension}',
    )


def _write_private_file(path: Path, content: str) -> None:
    path.write_text(content, encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remote_path(service: str, requested_path: str | None) -> str:
    if service in ('haproxy', 'keepalived'):
        path = sql.get_setting(f'{service}_config_path')
    else:
        path = config_mod._replace_config_path_to_correct(requested_path)
    common.check_is_conf(path)
    return path


def _require_server_service(server, service: str) -> None:
    if not int(getattr(server, service, 0)):
        raise RoxywiValidationError(f'{service.title()} is not enabled for this server')


def _require_change_group(change, group_id: int) -> None:
    if int(change.group_id) != int(group_id):
        raise RoxywiPermissionError('Change does not belong to the active group')


def _has_error(output: str | None) -> bool:
    return bool(output and ERROR_PATTERN.search(output))


def _upload(change, local_path: str, action: str) -> str:
    server = server_sql.get_server(change.server_id)
    if action == 'test':
        validation_results = []
        for target in _target_servers(server):
            output = config_mod.validate_candidate_config(
                target.ip,
                local_path,
                change.service,
                config_file_name=change.remote_path,
            )
            validation_results.append(f'{target.hostname}: {output}')
        return '\n'.join(validation_results)
    kwargs = {
        'config_file_name': change.remote_path,
        'oldcfg': change.rollback_path,
        'record_version': False,
    }
    if change.service == 'keepalived':
        output = config_mod.upload_and_restart(server.ip, local_path, action, change.service, **kwargs)
    else:
        output = config_mod.master_slave_upload_and_restart(
            server.ip, local_path, action, change.service, **kwargs
        )
    output = str(output or '')
    if _has_error(output):
        raise RuntimeError(output)
    return output


def _target_servers(server) -> list:
    targets = [server]
    for slave_ip, _slave_hostname in server_sql.is_master(server.ip):
        if slave_ip:
            targets.append(server_sql.get_server_by_ip(slave_ip))
    return targets


def _rollout_servers(server, service: str) -> list[tuple[object, str]]:
    """Return the existing propagation topology in safe apply order."""
    if service == 'keepalived':
        return [(server, 'standalone')]

    slaves = []
    for slave_ip, _slave_hostname in server_sql.is_master(server.ip):
        if slave_ip:
            slaves.append(server_sql.get_server_by_ip(slave_ip))
    slaves.sort(key=lambda item: (int(item.pos or 0), int(item.server_id)))
    primary_role = 'master' if slaves else 'standalone'
    return [*((slave, 'slave') for slave in slaves), (server, primary_role)]


def _target_snapshot_path(rollback_path: str | Path, server_ip: str) -> Path:
    base_path = Path(rollback_path)
    return base_path.with_name(
        f'{base_path.stem}-before-{_safe_name(server_ip, "server")}-{uuid4().hex}{base_path.suffix}'
    )


def _capture_rollout_targets(
    server, service: str, remote_path: str, rollback_path: Path
) -> tuple[list[dict], list[Path]]:
    """Capture an independent pre-change snapshot for every rollout target."""
    topology = _rollout_servers(server, service)
    captured_paths = [rollback_path]
    try:
        config_mod.get_config(
            server.ip,
            str(rollback_path),
            service=service,
            config_file_name=remote_path,
        )
        try:
            os.chmod(rollback_path, 0o600)
        except OSError:
            pass

        targets = []
        for position, (target_server, role) in enumerate(topology):
            if int(target_server.server_id) == int(server.server_id):
                target_rollback_path = rollback_path
            else:
                target_rollback_path = _target_snapshot_path(rollback_path, target_server.ip)
                captured_paths.append(target_rollback_path)
                config_mod.get_config(
                    target_server.ip,
                    str(target_rollback_path),
                    service=service,
                    config_file_name=remote_path,
                )
                try:
                    os.chmod(target_rollback_path, 0o600)
                except OSError:
                    pass
            targets.append({
                'server_id': target_server.server_id,
                'server_ip': target_server.ip,
                'server_name': target_server.hostname,
                'role': role,
                'position': position,
                'status': 'pending',
                'rollback_path': str(target_rollback_path),
            })
        return targets, captured_paths
    except Exception:
        for captured_path in captured_paths:
            try:
                captured_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _ensure_rollout_targets(change) -> list:
    """Load rollout targets and lazily upgrade changes created before phase two."""
    targets = change_sql.list_targets(change.id)
    server = server_sql.get_server(change.server_id)
    expected_topology = _rollout_servers(server, change.service)

    if not targets:
        captured_paths = []
        values = []
        for position, (target_server, role) in enumerate(expected_topology):
            if int(target_server.server_id) == int(change.server_id):
                snapshot_path = Path(change.rollback_path)
            else:
                snapshot_path = _target_snapshot_path(change.rollback_path, target_server.ip)
                config_mod.get_config(
                    target_server.ip,
                    str(snapshot_path),
                    service=change.service,
                    config_file_name=change.remote_path,
                )
                captured_paths.append(snapshot_path)
                try:
                    os.chmod(snapshot_path, 0o600)
                except OSError:
                    pass
            values.append({
                'server_id': target_server.server_id,
                'server_ip': target_server.ip,
                'server_name': target_server.hostname,
                'role': role,
                'position': position,
                'status': 'pending',
                'rollback_path': str(snapshot_path),
            })
        try:
            targets = change_sql.create_targets(change.id, values)
        except Exception:
            for snapshot_path in captured_paths:
                try:
                    snapshot_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    expected_ids = [int(item.server_id) for item, _role in expected_topology]
    stored_ids = [int(target.server_id) for target in targets]
    if stored_ids != expected_ids:
        raise RoxywiConflictError(
            'The master/slave topology changed after this draft was created; create a new change'
        )
    for target, (target_server, expected_role) in zip(targets, expected_topology):
        if target.server_ip != target_server.ip or target.role != expected_role:
            raise RoxywiConflictError(
                'The master/slave topology changed after this draft was created; create a new change'
            )
    return targets


def _target_server(target):
    server = server_sql.get_server(target.server_id)
    if server.ip != target.server_ip:
        raise RoxywiConflictError(
            f'The address of rollout target {target.server_name} changed; create a new change'
        )
    return server


def _post_deploy_check(change) -> str:
    server = server_sql.get_server(change.server_id)
    results = []
    for target in _target_servers(server):
        service_common.check_service_config(target.ip, target.server_id, change.service)
        result = f'{target.hostname}: configuration is valid'
        if change.action in ('reload', 'restart'):
            if not _is_service_active(target, change.service):
                raise RuntimeError(f'{target.hostname}: {change.service} is not active after deployment')
            result += ', service is active'
        results.append(result)
    return '\n'.join(results)


def _is_service_active(target, service: str) -> bool:
    is_dockerized = service_sql.select_service_setting(target.server_id, service, 'dockerized')
    if is_dockerized == '1':
        container_name = sql.get_setting(f'{service}_container_name')
        command = f"sudo docker inspect -f '{{{{.State.Running}}}}' {quote(str(container_name))}"
        expected_state = 'true'
    else:
        service_name = service_common.get_correct_service_name(service, target.server_id)
        command = f'systemctl is-active {quote(service_name)}'
        expected_state = 'active'
    state = server_mod.ssh_command(target.ip, command)
    return ANSI_PATTERN.sub('', str(state or '')).strip().lower() == expected_state


def _ensure_action_ready(change) -> None:
    """Do not replace a configuration when reload cannot activate the service."""
    if change.action != 'reload':
        return
    targets = _ensure_rollout_targets(change)
    inactive_servers = []
    try:
        inactive_servers = [
            target.server_name for target in targets
            if not _is_service_active(_target_server(target), change.service)
        ]
    except Exception as exc:
        raise RoxywiValidationError(
            f'Cannot verify {change.service.title()} state before deployment: '
            f'{_short_operation_error(exc)}'
        ) from exc
    if inactive_servers:
        raise RoxywiConflictError(
            f'Cannot deploy with reload because {change.service.title()} is not active on '
            f'{", ".join(inactive_servers)}. Start the service or create the change with restart.'
        )


def _ensure_base_unchanged(change) -> None:
    """Refuse to overwrite a configuration changed after the draft snapshot."""
    targets = _ensure_rollout_targets(change)
    for target in targets:
        rollback_path = Path(target.rollback_path)
        if not rollback_path.is_file():
            raise RoxywiConflictError(
                f'The original configuration snapshot is no longer available for {target.server_name}'
            )
        current_path = rollback_path.with_name(
            f'{rollback_path.stem}-current-{_safe_name(target.server_ip, "server")}-{uuid4().hex}{rollback_path.suffix}'
        )
        try:
            config_mod.get_config(
                target.server_ip,
                str(current_path),
                service=change.service,
                config_file_name=change.remote_path,
            )
            if current_path.read_bytes() != rollback_path.read_bytes():
                raise RoxywiConflictError(
                    f'Configuration on {target.server_name} changed after this draft was created; create a new change'
                )
        finally:
            try:
                current_path.unlink()
            except FileNotFoundError:
                pass


def _reconcile_interrupted_rollout(change) -> list:
    """Resolve each target to original or candidate state before resuming."""
    targets = _ensure_rollout_targets(change)
    candidate_path = Path(change.draft_path)
    if not candidate_path.is_file():
        raise RoxywiConflictError('The candidate configuration is no longer available')
    candidate = candidate_path.read_bytes()

    for target in targets:
        rollback_path = Path(target.rollback_path)
        if not rollback_path.is_file():
            raise RoxywiConflictError(
                f'The original configuration snapshot is no longer available for {target.server_name}'
            )
        current_path = rollback_path.with_name(
            f'{rollback_path.stem}-resume-{_safe_name(target.server_ip, "server")}-{uuid4().hex}{rollback_path.suffix}'
        )
        try:
            config_mod.get_config(
                target.server_ip,
                str(current_path),
                service=change.service,
                config_file_name=change.remote_path,
            )
            current = current_path.read_bytes()
        finally:
            try:
                current_path.unlink()
            except FileNotFoundError:
                pass

        if current == candidate and target.status == 'deployed':
            _check_target(change, target, len(targets))
            continue
        if current == candidate:
            change_sql.update_target(
                target.id,
                status='pending',
                deployment_output=(
                    'Candidate configuration found after the interrupted deployment; '
                    'the service action will be retried.'
                ),
                rollback_output=None,
                deployed_at=None,
            )
            continue
        if current == rollback_path.read_bytes():
            change_sql.update_target(
                target.id,
                status='pending',
                deployment_output=None,
                rollback_output=None,
                deployed_at=None,
            )
            continue
        raise RoxywiConflictError(
            f'Configuration on {target.server_name} differs from both the original and '
            'candidate versions; create a new change'
        )
    return change_sql.list_targets(change.id)


def _version_path(change) -> Path:
    generated = Path(config_common.generate_config_path(
        change.service, server_sql.get_server(change.server_id).ip
    ))
    return generated.with_name(f'{generated.stem}-change-{change.id}{generated.suffix}')


def _save_successful_version(change, source_path: str, diff: str, message: str) -> None:
    version_path = _version_path(change)
    version_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, version_path)
    try:
        os.chmod(version_path, 0o600)
    except OSError:
        pass
    config_sql.insert_config_version(
        change.server_id,
        change.user_id,
        change.service,
        str(version_path),
        change.remote_path,
        diff,
        message=message,
    )


def _name_for_user(user_id: int | None) -> str | None:
    if not user_id:
        return None
    try:
        return user_sql.get_user_id(user_id).username
    except Exception:
        return None


def is_recoverable(change, now: datetime | None = None) -> bool:
    if change.status not in IN_PROGRESS_STATUSES:
        return False
    if not change.updated_at:
        return True
    if now is None:
        now = datetime.now(change.updated_at.tzinfo) if change.updated_at.tzinfo else datetime.now()
    return now - change.updated_at >= STALE_OPERATION_TIMEOUT


def _serialize_target(target) -> dict:
    return {
        'id': target.id,
        'server_id': target.server_id,
        'server_ip': target.server_ip,
        'server_name': target.server_name,
        'role': target.role,
        'position': target.position,
        'status': target.status,
        'validation_output': target.validation_output or '',
        'deployment_output': target.deployment_output or '',
        'rollback_output': target.rollback_output or '',
        'updated_at': target.updated_at.isoformat() if target.updated_at else None,
        'deployed_at': target.deployed_at.isoformat() if target.deployed_at else None,
    }


def serialize_change(change) -> dict:
    try:
        server = server_sql.get_server(change.server_id)
        server_name = server.hostname
        server_ip = server.ip
    except Exception:
        server_name = None
        server_ip = None
    return {
        'id': change.id,
        'server_id': change.server_id,
        'server_name': server_name,
        'server_ip': server_ip,
        'group_id': change.group_id,
        'user_id': change.user_id,
        'created_by': _name_for_user(change.user_id),
        'approved_by': change.approved_by,
        'approved_by_name': _name_for_user(change.approved_by),
        'service': change.service,
        'action': change.action,
        'execution_mode': change.execution_mode,
        'status': change.status,
        'title': change.title,
        'description': change.description or '',
        'remote_path': change.remote_path,
        'diff': change.diff or '',
        'validation_output': change.validation_output or '',
        'deployment_output': change.deployment_output or '',
        'rollback_output': change.rollback_output or '',
        'targets': [_serialize_target(target) for target in change_sql.list_targets(change.id)],
        'requires_approval': bool(change.requires_approval),
        'recoverable': is_recoverable(change),
        'created_at': change.created_at.isoformat() if change.created_at else None,
        'updated_at': change.updated_at.isoformat() if change.updated_at else None,
        'deployed_at': change.deployed_at.isoformat() if change.deployed_at else None,
    }


def create_change(body, user_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    if isinstance(body.server_id, int) or str(body.server_id).isdigit():
        server = server_sql.get_server(int(body.server_id))
    else:
        server = server_sql.get_server_by_ip(common.is_ip_or_dns(str(body.server_id)))
    if int(server.group_id) != int(group_id):
        raise RoxywiPermissionError('Server does not belong to the active group')
    _require_server_service(server, body.service)
    remote_path = _remote_path(body.service, body.file_path)
    draft_path, rollback_path = _change_paths(body.service, server.ip)
    captured_paths = []
    change = None
    try:
        target_values, captured_paths = _capture_rollout_targets(
            server,
            body.service,
            remote_path,
            rollback_path,
        )
        _write_private_file(draft_path, body.config)
        diff = config_mod.diff_config(str(rollback_path), str(draft_path))
        change = change_sql.create_change(
            server_id=server.server_id,
            group_id=group_id,
            user_id=user_id,
            service=body.service,
            action=body.action,
            execution_mode=body.execution_mode,
            status='draft',
            title=body.title,
            description=body.description,
            remote_path=remote_path,
            draft_path=str(draft_path),
            rollback_path=str(rollback_path),
            diff=diff,
            requires_approval=int(body.requires_approval),
        )
        change_sql.create_targets(change.id, target_values)
        return change
    except Exception:
        if change is not None:
            try:
                change.delete_instance()
            except Exception:
                pass
        for path in (draft_path, *captured_paths):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def update_change(change_id: int, body, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    if change.status not in EDITABLE_STATUSES:
        raise RoxywiConflictError('Only draft or failed validation changes can be edited')
    values = body.model_dump(exclude_unset=True)
    values.update(validation_output=None, approved_by=None, status='draft')
    return change_sql.update_change(change_id, **values)


def _upload_target(
    change, target, local_path: str, action: str, target_count: int, *, normalize_config: bool = True
) -> str:
    """Apply one configuration without implicitly touching any other server."""
    if target_count == 1 and change.service != 'keepalived':
        return _upload(change, local_path, action)
    output = config_mod.upload_and_restart(
        target.server_ip,
        local_path,
        action,
        change.service,
        config_file_name=change.remote_path,
        oldcfg=target.rollback_path,
        record_version=False,
        slave=target.role == 'slave',
        normalize_config=normalize_config,
        user_id=change.user_id,
    )
    output = str(output or change.service.title())
    if _has_error(output):
        raise RuntimeError(output)
    return output


def _check_target(change, target, target_count: int) -> str:
    if target_count == 1 and change.service != 'keepalived':
        return _post_deploy_check(change)
    target_server = _target_server(target)
    service_common.check_service_config(target.server_ip, target.server_id, change.service)
    result = 'Configuration is valid'
    if change.action in ('reload', 'restart'):
        if not _is_service_active(target_server, change.service):
            raise RuntimeError(f'{change.service} is not active after deployment')
        result += ', service is active'
    return result


def _target_operation_output(targets: list, field: str) -> str:
    if len(targets) == 1:
        return getattr(targets[0], field) or ''
    sections = []
    for target in targets:
        output = getattr(target, field) or ''
        sections.append(
            f'{target.server_name} ({target.role}) [{target.status}]' + (f'\n{output}' if output else '')
        )
    return '\n\n'.join(sections)


def validate_change(change_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    targets = _ensure_rollout_targets(change)
    change_sql.transition_change(change_id, VALIDATABLE_STATUSES, 'validating')
    failures = []
    for target in targets:
        change_sql.update_target(target.id, status='validating', validation_output=None)
        try:
            if len(targets) == 1 and change.service != 'keepalived':
                output = _upload(change, change.draft_path, 'test')
            else:
                output = config_mod.validate_candidate_config(
                    target.server_ip,
                    change.draft_path,
                    change.service,
                    config_file_name=change.remote_path,
                )
            change_sql.update_target(
                target.id,
                status='validated',
                validation_output=str(output or 'Configuration is valid'),
            )
        except Exception as exc:
            failures.append(f'{target.server_name}: {exc}')
            change_sql.update_target(
                target.id,
                status='validation_failed',
                validation_output=str(exc),
            )
    targets = change_sql.list_targets(change.id)
    output = _target_operation_output(targets, 'validation_output')
    if failures:
        change_sql.update_change(change_id, status='validation_failed', validation_output=output)
        raise RoxywiValidationError(
            f'Configuration validation failed: {_short_operation_error(failures[0])}'
        )
    status = 'pending_approval' if change.requires_approval else 'validated'
    return change_sql.update_change(change_id, status=status, validation_output=output or 'Configuration is valid')


def approve_change(change_id: int, approver_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    if not change.requires_approval:
        raise RoxywiConflictError('This change does not require approval')
    if int(change.user_id) == int(approver_id):
        raise RoxywiPermissionError('The author cannot approve their own change')
    return change_sql.transition_change(
        change_id, ('pending_approval',), 'approved', approved_by=approver_id
    )


def _apply_target(
    change, target, local_path: str, target_count: int, *, normalize_config: bool = True
) -> str:
    upload_output = _upload_target(
        change,
        target,
        local_path,
        change.action,
        target_count,
        normalize_config=normalize_config,
    )
    health_output = _check_target(change, target, target_count)
    return '\n'.join(filter(None, (upload_output, health_output)))


def _deploy_rollout_rolling(change, targets: list) -> str:
    affected_target_ids = [target.id for target in targets if target.status == 'deployed']
    for index, target in enumerate(targets):
        if target.status == 'deployed':
            continue
        affected_target_ids.append(target.id)
        change_sql.update_target(target.id, status='deploying')
        try:
            output = _apply_target(change, target, change.draft_path, len(targets))
            change_sql.update_target(
                target.id,
                status='deployed',
                deployment_output=output,
                deployed_at=datetime.now(),
            )
        except Exception as exc:
            change_sql.update_target(
                target.id,
                status='deployment_failed',
                deployment_output=str(exc),
            )
            for skipped_target in targets[index + 1:]:
                if skipped_target.status != 'deployed':
                    change_sql.update_target(skipped_target.id, status='skipped')
            raise _RolloutFailure(
                f'{target.server_name}: {exc}', affected_target_ids
            ) from exc
    return _target_operation_output(change_sql.list_targets(change.id), 'deployment_output')


def _deploy_rollout_parallel(change, targets: list) -> str:
    pending_targets = [target for target in targets if target.status != 'deployed']
    affected_target_ids = [target.id for target in targets if target.status == 'deployed']
    if not pending_targets:
        return _target_operation_output(targets, 'deployment_output')

    config_mod.normalize_config_file(change.draft_path)
    for target in pending_targets:
        affected_target_ids.append(target.id)
        change_sql.update_target(target.id, status='deploying')

    failures = {}
    worker_count = min(8, len(pending_targets))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix='change-rollout') as executor:
        futures = {
            executor.submit(
                _apply_target,
                change,
                target,
                change.draft_path,
                len(targets),
                normalize_config=False,
            ): target
            for target in pending_targets
        }
        for future in as_completed(futures):
            target = futures[future]
            try:
                output = future.result()
                change_sql.update_target(
                    target.id,
                    status='deployed',
                    deployment_output=output,
                    deployed_at=datetime.now(),
                )
            except Exception as exc:
                failures[target.id] = exc
                change_sql.update_target(
                    target.id,
                    status='deployment_failed',
                    deployment_output=str(exc),
                )

    if failures:
        failed_target = next(target for target in pending_targets if target.id in failures)
        raise _RolloutFailure(
            f'{failed_target.server_name}: {failures[failed_target.id]}', affected_target_ids
        ) from failures[failed_target.id]
    return _target_operation_output(change_sql.list_targets(change.id), 'deployment_output')


def _deploy_rollout(change, *, resume: bool = False) -> str:
    targets = (
        change_sql.list_targets(change.id)
        if resume
        else change_sql.reset_targets(change.id)
    )
    if change.execution_mode == 'parallel' and len(targets) > 1:
        return _deploy_rollout_parallel(change, targets)
    return _deploy_rollout_rolling(change, targets)


def _rollback_targets(change, target_ids: list[int]) -> tuple[str, list[str]]:
    all_targets = change_sql.list_targets(change.id)
    target_id_set = set(target_ids)
    targets = [
        target for target in all_targets
        if target.id in target_id_set
    ]
    target_count = len(all_targets)
    failures = []
    for target in reversed(targets):
        change_sql.update_target(target.id, status='rolling_back')
        try:
            output = _apply_target(change, target, target.rollback_path, target_count)
            change_sql.update_target(
                target.id,
                status='rolled_back',
                rollback_output=output,
            )
        except Exception as exc:
            failures.append(f'{target.server_name}: {exc}')
            change_sql.update_target(
                target.id,
                status='rollback_failed',
                rollback_output=str(exc),
            )
    current_targets = change_sql.list_targets(change.id)
    return _target_operation_output(current_targets, 'rollback_output'), failures


def _rollback_after_failure(change, deployment_error: str, target_ids: list[int] | None = None):
    targets = change_sql.list_targets(change.id)
    if target_ids is None:
        target_ids = [
            target.id for target in targets
            if target.status in ('deployed', 'deploying', 'deployment_failed')
        ]
    rollback_output, failures = _rollback_targets(change, target_ids)
    return change_sql.update_change(
        change.id,
        status='auto_rollback_failed' if failures else 'auto_rolled_back',
        deployment_output=deployment_error,
        rollback_output=rollback_output,
    )


def _short_operation_error(error: Exception | str) -> str:
    output = ANSI_PATTERN.sub('', str(error)).replace('<br>', '\n')
    inactive_match = re.search(
        r'([A-Za-z0-9_.@-]+\.service is not active, cannot reload\.?)', output, re.IGNORECASE
    )
    if inactive_match:
        return inactive_match.group(1)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    useful_lines = [
        line for line in lines
        if '[NOTICE]' not in line and '[WARNING]' not in line and 'config : parsing' not in line
    ]
    summary = (useful_lines or lines or ['Unknown deployment error'])[-1]
    return summary if len(summary) <= 240 else f'{summary[:237]}...'


def deploy_change(change_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    ready_status = 'approved' if change.requires_approval else 'validated'
    allowed_status = (ready_status, *RETRYABLE_DEPLOY_STATUSES)
    if change.status not in allowed_status:
        raise RoxywiConflictError('The change is not ready for deployment')
    resume = change.status == 'deployment_interrupted'
    _ensure_rollout_targets(change)
    if resume:
        _reconcile_interrupted_rollout(change)
    else:
        _ensure_base_unchanged(change)
    _ensure_action_ready(change)
    change = change_sql.transition_change(change_id, allowed_status, 'deploying')
    try:
        deployment_output = _deploy_rollout(change, resume=resume)
        _save_successful_version(
            change, change.draft_path, change.diff, f'Change #{change.id}: {change.title}'
        )
    except Exception as exc:
        affected_target_ids = getattr(exc, 'affected_target_ids', None)
        failed_change = _rollback_after_failure(change, str(exc), affected_target_ids)
        if failed_change.status == 'auto_rolled_back':
            rollback_message = 'The previous configuration was restored automatically.'
        else:
            rollback_message = 'Automatic rollback also failed. Open change details for the full output.'
        raise RoxywiValidationError(
            f'Deployment failed. {rollback_message} Reason: {_short_operation_error(exc)}'
        ) from exc
    return change_sql.update_change(
        change_id,
        status='deployed',
        deployment_output=deployment_output,
        deployed_at=datetime.now(),
    )


def rollback_change(change_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    previous_status = change.status
    targets = _ensure_rollout_targets(change)
    if previous_status == 'deployed':
        target_ids = [target.id for target in targets]
    else:
        target_ids = [
            target.id for target in targets
            if target.status in (
                'deployed', 'deploying', 'deployment_failed', 'deployment_interrupted',
                'rollback_failed', 'rolling_back',
            )
        ]
    if not target_ids:
        raise RoxywiConflictError('No changed rollout targets are available for rollback')
    change = change_sql.transition_change(
        change_id,
        ('deployed', 'rollback_failed', 'auto_rollback_failed', 'deployment_interrupted'),
        'rolling_back',
    )
    try:
        rollback_output, failures = _rollback_targets(change, target_ids)
        if failures:
            raise RuntimeError('; '.join(failures))
        primary_was_affected = any(
            target.id in target_ids and int(target.server_id) == int(change.server_id)
            for target in targets
        )
        if previous_status == 'deployed' or primary_was_affected:
            reverse_diff = config_mod.diff_config(change.draft_path, change.rollback_path)
            _save_successful_version(
                change, change.rollback_path, reverse_diff,
                f'Rollback of change #{change.id}: {change.title}'
            )
    except Exception as exc:
        current_output = _target_operation_output(
            change_sql.list_targets(change.id), 'rollback_output'
        )
        change_sql.update_change(
            change_id,
            status='rollback_failed',
            rollback_output=current_output or str(exc),
        )
        raise RoxywiValidationError(
            f'Rollback failed: {_short_operation_error(exc)}'
        ) from exc
    return change_sql.update_change(change_id, status='rolled_back', rollback_output=rollback_output)


def cancel_change(change_id: int, group_id: int):
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    return change_sql.transition_change(change_id, CANCELLABLE_STATUSES, 'cancelled')


def recover_change(change_id: int, group_id: int):
    """Unlock an abandoned workflow operation without touching the remote configuration."""
    require_feature(CHANGE_CENTER)
    change = change_sql.get_change(change_id)
    _require_change_group(change, group_id)
    if change.status not in IN_PROGRESS_STATUSES:
        raise RoxywiConflictError('Only an in-progress operation can be recovered')
    if not is_recoverable(change):
        raise RoxywiConflictError(
            'The operation is still within the five-minute recovery timeout'
        )
    recovery = {
        'validating': (
            'validation_failed',
            'validation_output',
            'Validation was interrupted and unlocked. Run validation again.',
        ),
        'deploying': (
            'deployment_interrupted',
            'deployment_output',
            'Deployment was interrupted and unlocked. Verify the remote state, then deploy or roll back.',
        ),
        'rolling_back': (
            'rollback_failed',
            'rollback_output',
            'Rollback was interrupted and unlocked. Run rollback again.',
        ),
    }
    status, output_field, recovery_message = recovery[change.status]
    target_status = {
        'validating': 'validation_failed',
        'deploying': 'deployment_interrupted',
        'rolling_back': 'rollback_failed',
    }[change.status]
    target_output_field = {
        'validating': 'validation_output',
        'deploying': 'deployment_output',
        'rolling_back': 'rollback_output',
    }[change.status]
    for target in change_sql.list_targets(change.id):
        if target.status == change.status:
            target_output = '\n'.join(filter(None, (
                getattr(target, target_output_field) or '',
                recovery_message,
            )))
            change_sql.update_target(
                target.id,
                status=target_status,
                **{target_output_field: target_output},
            )
    previous_output = getattr(change, output_field) or ''
    output = '\n'.join(filter(None, (previous_output, recovery_message)))
    return change_sql.transition_change(
        change_id,
        (change.status,),
        status,
        **{output_field: output},
    )
