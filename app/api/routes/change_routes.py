from flask import g, jsonify
from flask_jwt_extended import jwt_required
from flask_pydantic import validate

import app.modules.change.service as change_service
import app.modules.db.change as change_sql
import app.modules.roxywi.auth as roxywi_auth
import app.modules.roxywi.common as roxywi_common
from app.api.routes import bp
from app.middleware import check_group, get_user_params, page_for_admin
from app.modules.change.access import change_center_subscription_required
from app.modules.change.schemas import ConfigChangeCreate, ConfigChangeQuery, ConfigChangeUpdate
from app.modules.roxywi.exception import (
    RoxywiConflictError,
    RoxywiPermissionError,
    RoxywiResourceNotFound,
    RoxywiValidationError,
)


CHANGE_STATUSES = [
    'draft', 'validating', 'validated', 'validation_failed', 'pending_approval',
    'approved', 'deploying', 'deployment_interrupted', 'deployed', 'auto_rolled_back',
    'auto_rollback_failed', 'rolling_back', 'rolled_back', 'rollback_failed',
    'cancelled',
]


def extend_spec(spec: dict) -> None:
    """Add shared Change Center schemas and bearer authentication to Swagger."""
    tags = spec.setdefault('tags', [])
    if not any(tag.get('name') == 'Configuration changes' for tag in tags):
        tags.append({
            'name': 'Configuration changes',
            'description': 'Validate, approve, deploy and roll back managed service configurations.',
        })
    spec.setdefault('securityDefinitions', {}).setdefault('BearerAuth', {
        'type': 'apiKey',
        'name': 'Authorization',
        'in': 'header',
        'description': 'JWT returned by /api/login. Use: Bearer <token>',
    })
    spec.setdefault('definitions', {}).update({
        'ConfigChangeTarget': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'server_id': {'type': 'integer'},
                'server_name': {'type': 'string'},
                'server_ip': {'type': 'string'},
                'role': {'type': 'string', 'enum': ['slave', 'master', 'standalone']},
                'position': {'type': 'integer'},
                'status': {
                    'type': 'string',
                    'enum': [
                        'pending', 'validating', 'validated', 'validation_failed',
                        'deploying', 'deployed', 'deployment_failed',
                        'deployment_interrupted', 'skipped', 'rolling_back',
                        'rolled_back', 'rollback_failed',
                    ],
                },
                'validation_output': {'type': 'string'},
                'deployment_output': {'type': 'string'},
                'rollback_output': {'type': 'string'},
                'updated_at': {'type': 'string', 'format': 'date-time'},
                'deployed_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ConfigChangeCreate': {
            'type': 'object',
            'required': ['server_id', 'service', 'config', 'title'],
            'properties': {
                'server_id': {'description': 'Managed server ID or IP address', 'type': 'string'},
                'service': {
                    'type': 'string',
                    'enum': ['haproxy', 'nginx', 'apache', 'keepalived'],
                },
                'action': {
                    'type': 'string',
                    'enum': ['save', 'reload', 'restart'],
                    'default': 'reload',
                },
                'execution_mode': {
                    'type': 'string',
                    'enum': ['rolling', 'parallel'],
                    'default': 'rolling',
                    'description': 'Rolling applies nodes sequentially; parallel applies up to eight nodes concurrently',
                },
                'config': {'type': 'string', 'description': 'Complete candidate configuration'},
                'file_path': {
                    'type': 'string',
                    'description': 'Required for NGINX and Apache multi-file configurations',
                },
                'title': {'type': 'string', 'maxLength': 255},
                'description': {'type': 'string', 'maxLength': 4000},
                'requires_approval': {'type': 'boolean', 'default': False},
            },
        },
        'ConfigChangeUpdate': {
            'type': 'object',
            'minProperties': 1,
            'properties': {
                'title': {'type': 'string', 'maxLength': 255},
                'description': {'type': 'string', 'maxLength': 4000},
                'action': {'type': 'string', 'enum': ['save', 'reload', 'restart']},
                'execution_mode': {'type': 'string', 'enum': ['rolling', 'parallel']},
            },
        },
        'ConfigChange': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'server_id': {'type': 'integer'},
                'server_name': {'type': 'string'},
                'server_ip': {'type': 'string'},
                'group_id': {'type': 'integer'},
                'user_id': {'type': 'integer'},
                'created_by': {'type': 'string'},
                'approved_by': {'type': 'integer'},
                'approved_by_name': {'type': 'string'},
                'service': {
                    'type': 'string',
                    'enum': ['haproxy', 'nginx', 'apache', 'keepalived'],
                },
                'action': {'type': 'string', 'enum': ['save', 'reload', 'restart']},
                'execution_mode': {'type': 'string', 'enum': ['rolling', 'parallel']},
                'status': {'type': 'string', 'enum': CHANGE_STATUSES},
                'title': {'type': 'string'},
                'description': {'type': 'string'},
                'remote_path': {'type': 'string'},
                'diff': {'type': 'string'},
                'validation_output': {'type': 'string'},
                'deployment_output': {'type': 'string'},
                'rollback_output': {'type': 'string'},
                'targets': {
                    'type': 'array',
                    'items': {'$ref': '#/definitions/ConfigChangeTarget'},
                },
                'requires_approval': {'type': 'boolean'},
                'created_at': {'type': 'string', 'format': 'date-time'},
                'updated_at': {'type': 'string', 'format': 'date-time'},
                'deployed_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ConfigChangeResponse': {
            'type': 'object',
            'properties': {
                'status': {'type': 'string', 'enum': ['success']},
                'data': {'$ref': '#/definitions/ConfigChange'},
            },
        },
        'ConfigChangeListResponse': {
            'type': 'object',
            'properties': {
                'status': {'type': 'string', 'enum': ['success']},
                'data': {
                    'type': 'array',
                    'items': {'$ref': '#/definitions/ConfigChange'},
                },
            },
        },
        'ConfigChangeError': {
            'type': 'object',
            'properties': {
                'status': {'type': 'string', 'enum': ['failed']},
                'error': {'type': 'string'},
            },
        },
    })


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
        roxywi_common.logging('Roxy-WI server', f'error: Change Center API operation failed: {exc}')
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


def _success(change, status_code: int = 200):
    return jsonify({
        'status': 'success',
        'data': change_service.serialize_change(change),
    }), status_code


def _run_action(change_id: int, action: str):
    try:
        change = _get_active_group_change(change_id)
        if action == 'approve':
            if int(g.user_params['role']) > 2:
                raise RoxywiPermissionError('Only administrators can approve changes')
            change = change_service.approve_change(
                change_id,
                g.user_params['user_id'],
                g.user_params['group_id'],
            )
        else:
            handler = getattr(change_service, f'{action}_change')
            change = handler(change_id, g.user_params['group_id'])
        if action in ('deploy', 'rollback', 'recover'):
            roxywi_common.logging(
                change.server_id,
                f'Configuration change #{change.id}: {action}',
                service=change.service,
            )
        return _success(change)
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(query=ConfigChangeQuery)
def list_config_changes(query: ConfigChangeQuery):
    """
    List configuration changes from the active group.
    ---
    tags: [Configuration changes]
    summary: List configuration changes
    security: [{BearerAuth: []}]
    parameters:
      - {name: service, in: query, type: string, required: false, enum: [haproxy, nginx, apache, keepalived]}
      - name: status
        in: query
        type: string
        required: false
        description: Filter by workflow status.
    responses:
      200: {description: Changes returned, schema: {$ref: '#/definitions/ConfigChangeListResponse'}}
      401: {description: Authentication or subscription error, schema: {$ref: '#/definitions/ConfigChangeError'}}
      403: {description: Insufficient permissions, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        changes = change_sql.list_changes(
            g.user_params['group_id'],
            service=query.service,
            status=query.status,
        )
        changes = [
            item for item in changes
            if roxywi_auth.is_access_permit_to_service(item.service)
        ]
        return jsonify({
            'status': 'success',
            'data': [change_service.serialize_change(item) for item in changes],
        })
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(body=ConfigChangeCreate)
def create_config_change(body: ConfigChangeCreate):
    """
    Create a configuration change draft.
    ---
    tags: [Configuration changes]
    summary: Create configuration change
    security: [{BearerAuth: []}]
    parameters:
      - name: body
        in: body
        required: true
        schema: {$ref: '#/definitions/ConfigChangeCreate'}
    responses:
      201: {description: Draft created, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      400: {description: Invalid request or configuration, schema: {$ref: '#/definitions/ConfigChangeError'}}
      403: {description: Server, group or service access denied, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        if not roxywi_auth.is_access_permit_to_service(body.service):
            raise RoxywiPermissionError(f'No access to {body.service.title()} changes')
        change = change_service.create_change(
            body,
            g.user_params['user_id'],
            g.user_params['group_id'],
        )
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id} has been created through API',
            service=change.service,
        )
        return _success(change, 201)
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/<int:change_id>')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def get_config_change(change_id: int):
    """
    Get one configuration change.
    ---
    tags: [Configuration changes]
    summary: Get configuration change
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
    responses:
      200: {description: Change returned, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      403: {description: Group or service access denied, schema: {$ref: '#/definitions/ConfigChangeError'}}
      404: {description: Change not found, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        return _success(_get_active_group_change(change_id))
    except Exception as exc:
        return _error_response(exc)


@bp.put('/changes/<int:change_id>')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(body=ConfigChangeUpdate)
def update_config_change(change_id: int, body: ConfigChangeUpdate):
    """
    Update a draft or validation-failed change.
    ---
    tags: [Configuration changes]
    summary: Update configuration change
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
      - {name: body, in: body, required: true, schema: {$ref: '#/definitions/ConfigChangeUpdate'}}
    responses:
      200: {description: Change updated, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      400: {description: Invalid request, schema: {$ref: '#/definitions/ConfigChangeError'}}
      409: {description: Current status does not allow editing, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        _get_active_group_change(change_id)
        change = change_service.update_change(change_id, body, g.user_params['group_id'])
        return _success(change)
    except Exception as exc:
        return _error_response(exc)


def _action_doc(summary: str) -> str:
    return f"""{summary}.
    ---
    tags: [Configuration changes]
    summary: {summary}
    security: [{{BearerAuth: []}}]
    parameters:
      - {{name: change_id, in: path, type: integer, required: true}}
    responses:
      200: {{description: Operation completed, schema: {{$ref: '#/definitions/ConfigChangeResponse'}}}}
      400: {{description: Validation or deployment error, schema: {{$ref: '#/definitions/ConfigChangeError'}}}}
      403: {{description: Permission denied, schema: {{$ref: '#/definitions/ConfigChangeError'}}}}
      404: {{description: Change not found, schema: {{$ref: '#/definitions/ConfigChangeError'}}}}
      409: {{description: Invalid workflow transition, schema: {{$ref: '#/definitions/ConfigChangeError'}}}}
    """


@bp.post('/changes/<int:change_id>/validate')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def validate_config_change(change_id: int):
    return _run_action(change_id, 'validate')


validate_config_change.__doc__ = _action_doc('Validate configuration change')


@bp.post('/changes/<int:change_id>/approve')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def approve_config_change(change_id: int):
    return _run_action(change_id, 'approve')


approve_config_change.__doc__ = _action_doc('Approve configuration change as a distinct administrator')


@bp.post('/changes/<int:change_id>/deploy')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def deploy_config_change(change_id: int):
    return _run_action(change_id, 'deploy')


deploy_config_change.__doc__ = _action_doc('Deploy validated configuration change')


@bp.post('/changes/<int:change_id>/rollback')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def rollback_config_change(change_id: int):
    return _run_action(change_id, 'rollback')


rollback_config_change.__doc__ = _action_doc('Roll back deployed configuration change')


@bp.post('/changes/<int:change_id>/cancel')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def cancel_config_change(change_id: int):
    return _run_action(change_id, 'cancel')


cancel_config_change.__doc__ = _action_doc('Cancel configuration change')


@bp.post('/changes/<int:change_id>/recover')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def recover_config_change(change_id: int):
    return _run_action(change_id, 'recover')


recover_config_change.__doc__ = _action_doc('Recover stale configuration change operation')
