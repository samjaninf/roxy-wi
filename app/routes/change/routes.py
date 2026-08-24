from flask import abort, g, jsonify, render_template, request
from flask_jwt_extended import jwt_required
from flask_pydantic import validate

import app.modules.change.service as change_service
import app.modules.db.change as change_sql
import app.modules.roxywi.common as roxywi_common
import app.modules.roxywi.auth as roxywi_auth
from app.middleware import get_user_params, page_for_admin
from app.modules.change.access import change_center_subscription_required
from app.modules.change.schemas import ConfigChangeCreate, ConfigChangeUpdate
from app.modules.roxywi.exception import (
    RoxywiConflictError,
    RoxywiPermissionError,
    RoxywiResourceNotFound,
    RoxywiValidationError,
)
from app.routes.change import bp


@bp.before_request
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
def before_request():
    """Authenticate Change Center pages and endpoints."""
    pass


def _error_response(exc: Exception):
    error_message = str(exc)
    if isinstance(exc, RoxywiResourceNotFound):
        status_code = 404
    elif isinstance(exc, RoxywiPermissionError):
        status_code = 403
    elif isinstance(exc, RoxywiConflictError):
        status_code = 409
    elif isinstance(exc, (RoxywiValidationError, ValueError)):
        status_code = 400
    else:
        roxywi_common.logging('Roxy-WI server', f'error: Change Center operation failed: {exc}')
        status_code = 500
        error_message = 'Internal server error'
    return jsonify({'status': 'failed', 'error': error_message}), status_code


def _get_active_group_change(change_id: int):
    change = change_sql.get_change(change_id)
    if int(change.group_id) != int(g.user_params['group_id']):
        raise RoxywiPermissionError('Change does not belong to the active group')
    if not roxywi_auth.is_access_permit_to_service(change.service):
        raise RoxywiPermissionError(f'No access to {change.service.title()} changes')
    return change


@bp.get('')
def index():
    return render_template('change_center.html', lang=g.user_params['lang'])


@bp.get('/api')
@change_center_subscription_required
def list_changes():
    service = request.args.get('service') or None
    status = request.args.get('status') or None
    if service and service not in ('haproxy', 'nginx', 'apache', 'keepalived'):
        return _error_response(RoxywiValidationError('Unsupported service filter'))
    try:
        changes = change_sql.list_changes(g.user_params['group_id'], service=service, status=status)
        changes = [item for item in changes if roxywi_auth.is_access_permit_to_service(item.service)]
        return jsonify({'status': 'success', 'data': [change_service.serialize_change(item) for item in changes]})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api')
@change_center_subscription_required
@validate(body=ConfigChangeCreate)
def create_change(body: ConfigChangeCreate):
    try:
        if not roxywi_auth.is_access_permit_to_service(body.service):
            raise RoxywiPermissionError(f'No access to {body.service.title()} changes')
        change = change_service.create_change(body, g.user_params['user_id'], g.user_params['group_id'])
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id} has been created',
            service=change.service,
        )
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)}), 201
    except Exception as exc:
        return _error_response(exc)


@bp.get('/api/<int:change_id>')
@change_center_subscription_required
def get_change(change_id: int):
    try:
        return jsonify({'status': 'success', 'data': change_service.serialize_change(
            _get_active_group_change(change_id)
        )})
    except Exception as exc:
        return _error_response(exc)


@bp.put('/api/<int:change_id>')
@change_center_subscription_required
@validate(body=ConfigChangeUpdate)
def update_change(change_id: int, body: ConfigChangeUpdate):
    try:
        _get_active_group_change(change_id)
        change = change_service.update_change(change_id, body, g.user_params['group_id'])
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/validate')
@change_center_subscription_required
def validate_change(change_id: int):
    try:
        _get_active_group_change(change_id)
        change = change_service.validate_change(change_id, g.user_params['group_id'])
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/approve')
@change_center_subscription_required
def approve_change(change_id: int):
    if int(g.user_params['role']) > 2:
        abort(403, 'Only administrators can approve changes')
    try:
        _get_active_group_change(change_id)
        change = change_service.approve_change(
            change_id, g.user_params['user_id'], g.user_params['group_id']
        )
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/deploy')
@change_center_subscription_required
def deploy_change(change_id: int):
    try:
        _get_active_group_change(change_id)
        change = change_service.deploy_change(change_id, g.user_params['group_id'])
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id} has been deployed',
            service=change.service,
        )
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/rollback')
@change_center_subscription_required
def rollback_change(change_id: int):
    try:
        _get_active_group_change(change_id)
        change = change_service.rollback_change(change_id, g.user_params['group_id'])
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id} has been rolled back',
            service=change.service,
        )
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/cancel')
@change_center_subscription_required
def cancel_change(change_id: int):
    try:
        _get_active_group_change(change_id)
        change = change_service.cancel_change(change_id, g.user_params['group_id'])
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/api/<int:change_id>/recover')
@change_center_subscription_required
def recover_change(change_id: int):
    try:
        _get_active_group_change(change_id)
        change = change_service.recover_change(change_id, g.user_params['group_id'])
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id} stale operation has been recovered',
            service=change.service,
        )
        return jsonify({'status': 'success', 'data': change_service.serialize_change(change)})
    except Exception as exc:
        return _error_response(exc)
