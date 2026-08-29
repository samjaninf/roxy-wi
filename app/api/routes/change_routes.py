from flask import Response, g, jsonify, request
from flask_jwt_extended import jwt_required
from flask_pydantic import validate

import app.modules.change.service as change_service
import app.modules.change.automation as change_automation
import app.modules.db.change as change_sql
import app.modules.roxywi.auth as roxywi_auth
import app.modules.roxywi.common as roxywi_common
from app.api.routes import bp
from app.middleware import check_group, get_user_params, page_for_admin
from app.modules.change.access import change_center_subscription_required
from app.modules.change.schemas import (
    ConfigChangeCreate,
    ConfigChangeAuditQuery,
    ConfigChangeQuery,
    ConfigChangeReportQuery,
    ConfigChangeSchedule,
    ConfigChangeStatisticsQuery,
    ConfigChangeTargetUpdate,
    ConfigChangeUpdate,
    ConfigChangeWebhookCreate,
    ConfigChangeWebhookUpdate,
)
from app.modules.roxywi.exception import (
    RoxywiConflictError,
    RoxywiPermissionError,
    RoxywiResourceNotFound,
    RoxywiValidationError,
)


CHANGE_STATUSES = [
    'draft', 'validating', 'validated', 'validation_failed', 'pending_approval',
    'approved', 'scheduled', 'schedule_missed', 'deploying', 'pause_requested',
    'paused', 'awaiting_promotion',
    'deployment_interrupted', 'deployed', 'auto_rolled_back',
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
                'batch': {'type': 'integer'},
                'is_canary': {'type': 'boolean'},
                'excluded': {'type': 'boolean'},
                'excluded_reason': {'type': 'string'},
                'status': {
                    'type': 'string',
                    'enum': [
                        'pending', 'validating', 'validated', 'validation_failed',
                        'deploying', 'deployed', 'deployment_failed',
                        'deployment_interrupted', 'skipped', 'excluded', 'rolling_back',
                        'rolled_back', 'rollback_failed',
                    ],
                },
                'validation_output': {'type': 'string'},
                'deployment_output': {'type': 'string'},
                'health_output': {'type': 'string'},
                'rollback_output': {'type': 'string'},
                'drift_status': {'type': 'string', 'enum': ['unknown', 'in_sync', 'drifted', 'check_failed']},
                'drift_checked_at': {'type': 'string', 'format': 'date-time'},
                'drift_diff': {'type': 'string'},
                'updated_at': {'type': 'string', 'format': 'date-time'},
                'deployed_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'NotificationDestination': {
            'type': 'object',
            'required': ['channel', 'recipient_id'],
            'properties': {
                'channel': {
                    'type': 'string',
                    'enum': ['email', 'telegram', 'slack', 'mm', 'pd'],
                },
                'recipient_id': {'type': 'integer', 'minimum': 1},
            },
        },
        'NotificationDestinationOption': {
            'allOf': [
                {'$ref': '#/definitions/NotificationDestination'},
                {
                    'type': 'object',
                    'properties': {
                        'channel_label': {'type': 'string'},
                        'label': {'type': 'string'},
                        'destination': {'type': 'string'},
                    },
                },
            ],
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
                'batch_size': {
                    'type': 'integer', 'minimum': 1, 'maximum': 50,
                    'description': 'Nodes per rollout batch; omit for mode default',
                },
                'max_parallel': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'default': 8},
                'manual_promotion': {'type': 'boolean', 'default': False},
                'health_check_mode': {
                    'type': 'string',
                    'enum': ['full', 'config', 'service', 'none'],
                    'default': 'full',
                },
                'health_check_retries': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 1},
                'health_check_interval': {'type': 'integer', 'minimum': 0, 'maximum': 60, 'default': 0},
                'canary_server_ids': {'type': 'array', 'items': {'type': 'integer'}},
                'excluded_server_ids': {'type': 'array', 'items': {'type': 'integer'}},
                'notification_channels': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': ['email', 'telegram', 'slack', 'mm', 'pd']},
                    'deprecated': True,
                    'description': 'Legacy group-wide notification selection; use notification_destinations',
                },
                'notification_destinations': {
                    'type': 'array',
                    'maxItems': 50,
                    'items': {'$ref': '#/definitions/NotificationDestination'},
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
                'batch_size': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                'max_parallel': {'type': 'integer', 'minimum': 1, 'maximum': 8},
                'manual_promotion': {'type': 'boolean'},
                'health_check_mode': {'type': 'string', 'enum': ['full', 'config', 'service', 'none']},
                'health_check_retries': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                'health_check_interval': {'type': 'integer', 'minimum': 0, 'maximum': 60},
                'canary_server_ids': {'type': 'array', 'items': {'type': 'integer'}},
                'excluded_server_ids': {'type': 'array', 'items': {'type': 'integer'}},
                'notification_channels': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': ['email', 'telegram', 'slack', 'mm', 'pd']},
                    'deprecated': True,
                },
                'notification_destinations': {
                    'type': 'array',
                    'maxItems': 50,
                    'items': {'$ref': '#/definitions/NotificationDestination'},
                },
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
                'batch_size': {'type': 'integer'},
                'effective_batch_size': {'type': 'integer'},
                'max_parallel': {'type': 'integer'},
                'manual_promotion': {'type': 'boolean'},
                'health_check_mode': {'type': 'string', 'enum': ['full', 'config', 'service', 'none']},
                'health_check_retries': {'type': 'integer'},
                'health_check_interval': {'type': 'integer'},
                'notification_channels': {'type': 'array', 'items': {'type': 'string'}},
                'notification_destinations': {
                    'type': 'array',
                    'items': {'$ref': '#/definitions/NotificationDestination'},
                },
                'scheduled_at': {'type': 'string', 'format': 'date-time'},
                'maintenance_window_end': {'type': 'string', 'format': 'date-time'},
                'drift_status': {'type': 'string', 'enum': ['unknown', 'in_sync', 'drifted', 'check_failed']},
                'drift_checked_at': {'type': 'string', 'format': 'date-time'},
                'drift_diff': {'type': 'string'},
                'started_at': {'type': 'string', 'format': 'date-time'},
                'finished_at': {'type': 'string', 'format': 'date-time'},
                'duration_seconds': {'type': 'number'},
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
        'ConfigChangeSchedule': {
            'type': 'object',
            'required': ['scheduled_at'],
            'properties': {
                'scheduled_at': {'type': 'string', 'format': 'date-time'},
                'maintenance_window_end': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ConfigChangeEvent': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'change_id': {'type': 'integer'},
                'change_title': {'type': 'string'},
                'service': {'type': 'string'},
                'target_id': {'type': 'integer'},
                'target_name': {'type': 'string'},
                'event_type': {'type': 'string'},
                'status': {'type': 'string'},
                'message': {'type': 'string'},
                'details': {'type': 'object'},
                'actor_id': {'type': 'integer'},
                'actor_name': {'type': 'string'},
                'created_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ConfigChangeWebhook': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'name': {'type': 'string'},
                'url': {'type': 'string'},
                'events': {'type': 'array', 'items': {'type': 'string'}},
                'enabled': {'type': 'boolean'},
                'verify_tls': {'type': 'boolean'},
                'secret_configured': {'type': 'boolean'},
                'created_at': {'type': 'string', 'format': 'date-time'},
                'updated_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ConfigChangeStatistics': {
            'type': 'object',
            'properties': {
                'period_days': {'type': 'integer'},
                'deployments': {'type': 'integer'},
                'successful': {'type': 'integer'},
                'failed': {'type': 'integer'},
                'success_rate': {'type': 'number'},
                'average_duration_seconds': {'type': 'number'},
                'scheduled': {'type': 'integer'},
                'drifted_targets': {'type': 'integer'},
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
            change = handler(
                change_id, g.user_params['group_id'], g.user_params['user_id']
            )
        if action in ('deploy', 'rollback', 'recover', 'pause', 'resume', 'promote'):
            roxywi_common.logging(
                change.server_id,
                f'Configuration change #{change.id}: {action}',
                service=change.service,
            )
        return _success(change)
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/rollout-preview')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def get_rollout_preview():
    """
    Preview the cluster rollout topology for a new change.
    ---
    tags: [Configuration changes]
    summary: Preview configuration rollout topology
    security: [{BearerAuth: []}]
    parameters:
      - {name: server_id, in: query, type: string, required: true}
      - {name: service, in: query, type: string, required: true, enum: [haproxy, nginx, apache, keepalived]}
    responses:
      200: {description: Rollout topology returned}
      400: {description: Invalid server or service, schema: {$ref: '#/definitions/ConfigChangeError'}}
      403: {description: Group or service access denied, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        server_id = request.args.get('server_id', '').strip()
        service = request.args.get('service', '').strip()
        if not server_id:
            raise RoxywiValidationError('server_id is required')
        if service not in ('haproxy', 'nginx', 'apache', 'keepalived'):
            raise RoxywiValidationError('Unsupported service')
        if not roxywi_auth.is_access_permit_to_service(service):
            raise RoxywiPermissionError(f'No access to {service.title()} changes')
        return jsonify({
            'status': 'success',
            'data': change_service.rollout_preview(
                server_id,
                service,
                g.user_params['group_id'],
            ),
        })
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/notification-destinations')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def get_notification_destinations():
    """
    List concrete notification recipients available to the active group.
    ---
    tags: [Configuration changes]
    summary: List Change Center notification recipients
    security: [{BearerAuth: []}]
    responses:
      200:
        description: Notification recipients returned without integration secrets
        schema:
          type: object
          properties:
            status: {type: string, enum: [success]}
            data:
              type: array
              items: {$ref: '#/definitions/NotificationDestinationOption'}
      403: {description: Group access denied, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        return jsonify({
            'status': 'success',
            'data': change_automation.list_notification_destinations(
                g.user_params['group_id']
            ),
        })
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
        change = change_service.update_change(
            change_id, body, g.user_params['group_id'], g.user_params['user_id']
        )
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


@bp.post('/changes/<int:change_id>/pause')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def pause_config_change(change_id: int):
    return _run_action(change_id, 'pause')


pause_config_change.__doc__ = _action_doc('Pause configuration rollout after the active batch')


@bp.post('/changes/<int:change_id>/resume')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def resume_config_change(change_id: int):
    return _run_action(change_id, 'resume')


resume_config_change.__doc__ = _action_doc('Resume paused configuration rollout')


@bp.post('/changes/<int:change_id>/promote')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def promote_config_change(change_id: int):
    return _run_action(change_id, 'promote')


promote_config_change.__doc__ = _action_doc('Promote configuration rollout to the next batch')


@bp.post('/changes/<int:change_id>/targets/<int:target_id>/<action>')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def run_config_change_target_action(change_id: int, target_id: int, action: str):
    """
    Run a per-node rollout action.
    ---
    tags: [Configuration changes]
    summary: Retry, roll back, exclude or include one rollout target
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
      - {name: target_id, in: path, type: integer, required: true}
      - {name: action, in: path, type: string, required: true, enum: [retry, rollback, exclude, include]}
      - name: body
        in: body
        required: false
        description: Optional reason used by the exclude action.
        schema:
          type: object
          properties:
            reason: {type: string, maxLength: 4000}
    responses:
      200: {description: Target action completed, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      400: {description: Target action failed, schema: {$ref: '#/definitions/ConfigChangeError'}}
      403: {description: Permission denied, schema: {$ref: '#/definitions/ConfigChangeError'}}
      404: {description: Change or target not found, schema: {$ref: '#/definitions/ConfigChangeError'}}
      409: {description: Action is not valid in the current state, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    handlers = {
        'retry': change_service.retry_target,
        'rollback': change_service.rollback_target,
        'exclude': change_service.exclude_target,
        'include': change_service.include_target,
    }
    try:
        change = _get_active_group_change(change_id)
        if action not in handlers:
            raise RoxywiResourceNotFound
        if action == 'exclude':
            body = ConfigChangeTargetUpdate.model_validate(request.get_json(silent=True) or {})
            change = handlers[action](
                change_id,
                target_id,
                g.user_params['group_id'],
                body.reason,
                g.user_params['user_id'],
            )
        else:
            change = handlers[action](
                change_id, target_id, g.user_params['group_id'], g.user_params['user_id']
            )
        roxywi_common.logging(
            change.server_id,
            f'Configuration change #{change.id}, target #{target_id}: {action}',
            service=change.service,
        )
        return _success(change)
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes/<int:change_id>/schedule')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(body=ConfigChangeSchedule)
def schedule_config_change(change_id: int, body: ConfigChangeSchedule):
    """Schedule a deployment.
    ---
    tags: [Configuration changes]
    summary: Schedule a validated configuration change within an optional maintenance window
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
      - {name: body, in: body, required: true, schema: {$ref: '#/definitions/ConfigChangeSchedule'}}
    responses:
      200: {description: Deployment scheduled, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      409: {description: Invalid workflow transition, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        _get_active_group_change(change_id)
        change = change_automation.schedule_change(
            change_id, body, g.user_params['group_id'], g.user_params['user_id']
        )
        return _success(change)
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes/<int:change_id>/schedule/cancel')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def cancel_config_change_schedule(change_id: int):
    """Cancel a scheduled deployment.
    ---
    tags: [Configuration changes]
    summary: Cancel a scheduled deployment and return it to its ready state
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
    responses:
      200: {description: Schedule cancelled, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      409: {description: Change is not scheduled, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        _get_active_group_change(change_id)
        return _success(change_automation.cancel_schedule(
            change_id, g.user_params['group_id'], g.user_params['user_id']
        ))
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes/<int:change_id>/drift')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def check_config_change_drift(change_id: int):
    """Check configuration drift.
    ---
    tags: [Configuration changes]
    summary: Compare deployed nodes with the approved configuration
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
    responses:
      200: {description: Drift check completed, schema: {$ref: '#/definitions/ConfigChangeResponse'}}
      409: {description: Change is not deployed, schema: {$ref: '#/definitions/ConfigChangeError'}}
    """
    try:
        _get_active_group_change(change_id)
        return _success(change_automation.check_change_drift(
            change_id, g.user_params['group_id'], g.user_params['user_id']
        ))
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/<int:change_id>/events')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(query=ConfigChangeAuditQuery)
def get_config_change_events(change_id: int, query: ConfigChangeAuditQuery):
    """Get a deployment timeline.
    ---
    tags: [Configuration changes]
    summary: Get the append-only timeline for one configuration change
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
      - {name: after_id, in: query, type: integer, required: false}
      - {name: limit, in: query, type: integer, required: false, default: 200}
    responses:
      200:
        description: Timeline returned
        schema:
          type: object
          properties:
            status: {type: string}
            data: {type: array, items: {$ref: '#/definitions/ConfigChangeEvent'}}
    """
    try:
        _get_active_group_change(change_id)
        return jsonify({
            'status': 'success',
            'data': change_automation.list_audit_events(
                g.user_params['group_id'], query, change_id=change_id
            ),
        })
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/audit')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(query=ConfigChangeAuditQuery)
def get_config_change_audit(query: ConfigChangeAuditQuery):
    """Search the Change Center audit history.
    ---
    tags: [Configuration changes]
    summary: Search configuration-change audit events
    security: [{BearerAuth: []}]
    parameters:
      - {name: q, in: query, type: string, required: false}
      - {name: service, in: query, type: string, required: false}
      - {name: event_type, in: query, type: string, required: false}
      - {name: status, in: query, type: string, required: false}
      - {name: date_from, in: query, type: string, format: date-time, required: false}
      - {name: date_to, in: query, type: string, format: date-time, required: false}
      - {name: limit, in: query, type: integer, default: 200}
    responses:
      200: {description: Matching audit events returned}
    """
    try:
        if query.service and not roxywi_auth.is_access_permit_to_service(query.service):
            raise RoxywiPermissionError(f'No access to {query.service.title()} changes')
        events = change_automation.list_audit_events(g.user_params['group_id'], query)
        events = [
            event for event in events
            if roxywi_auth.is_access_permit_to_service(event['service'])
        ]
        return jsonify({'status': 'success', 'data': events})
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/statistics')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(query=ConfigChangeStatisticsQuery)
def get_config_change_statistics(query: ConfigChangeStatisticsQuery):
    """Get deployment statistics.
    ---
    tags: [Configuration changes]
    summary: Get Change Center deployment duration, success rate and drift statistics
    security: [{BearerAuth: []}]
    parameters:
      - {name: days, in: query, type: integer, default: 30, minimum: 1, maximum: 365}
    responses:
      200:
        description: Statistics returned
        schema:
          type: object
          properties:
            status: {type: string}
            data: {$ref: '#/definitions/ConfigChangeStatistics'}
    """
    try:
        services = [
            service for service in ('haproxy', 'nginx', 'apache', 'keepalived')
            if roxywi_auth.is_access_permit_to_service(service)
        ]
        return jsonify({
            'status': 'success',
            'data': change_automation.deployment_statistics(
                g.user_params['group_id'], query.days, services=services
            ),
        })
    except Exception as exc:
        return _error_response(exc)


@bp.get('/changes/<int:change_id>/report')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(query=ConfigChangeReportQuery)
def export_config_change_report(change_id: int, query: ConfigChangeReportQuery):
    """Export a deployment report.
    ---
    tags: [Configuration changes]
    summary: Export detailed per-node deployment results as JSON or CSV
    security: [{BearerAuth: []}]
    parameters:
      - {name: change_id, in: path, type: integer, required: true}
      - {name: format, in: query, type: string, enum: [json, csv], default: json}
    produces: [application/json, text/csv]
    responses:
      200: {description: Deployment report returned}
    """
    try:
        change = _get_active_group_change(change_id)
        if query.format == 'csv':
            return Response(
                change_automation.build_change_report(change, as_csv=True),
                mimetype='text/csv',
                headers={
                    'Content-Disposition': f'attachment; filename="change-{change.id}-report.csv"'
                },
            )
        return jsonify({
            'status': 'success',
            'data': change_automation.build_change_report(change),
        })
    except Exception as exc:
        return _error_response(exc)


def _webhook_admin_required():
    if int(g.user_params['role']) > 2:
        raise RoxywiPermissionError('Only administrators can manage Change Center webhooks')


@bp.get('/changes/webhooks')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def get_config_change_webhooks():
    """List Change Center webhooks.
    ---
    tags: [Configuration changes]
    summary: List outbound automation webhooks for the active group
    security: [{BearerAuth: []}]
    responses:
      200: {description: Webhooks returned}
      403: {description: Administrator role required}
    """
    try:
        _webhook_admin_required()
        return jsonify({
            'status': 'success',
            'data': [
                change_automation.serialize_webhook(webhook)
                for webhook in change_sql.list_webhooks(g.user_params['group_id'])
            ],
        })
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes/webhooks')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(body=ConfigChangeWebhookCreate)
def create_config_change_webhook(body: ConfigChangeWebhookCreate):
    """Create a Change Center webhook.
    ---
    tags: [Configuration changes]
    summary: Create an outbound signed webhook
    security: [{BearerAuth: []}]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          allOf:
            - {$ref: '#/definitions/ConfigChangeWebhook'}
            - type: object
              properties:
                secret: {type: string, writeOnly: true}
    responses:
      201: {description: Webhook created}
    """
    try:
        _webhook_admin_required()
        webhook = change_automation.create_webhook(
            body, g.user_params['user_id'], g.user_params['group_id']
        )
        return jsonify({
            'status': 'success',
            'data': change_automation.serialize_webhook(webhook),
        }), 201
    except Exception as exc:
        return _error_response(exc)


@bp.put('/changes/webhooks/<int:webhook_id>')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
@validate(body=ConfigChangeWebhookUpdate)
def update_config_change_webhook(webhook_id: int, body: ConfigChangeWebhookUpdate):
    """Update a Change Center webhook.
    ---
    tags: [Configuration changes]
    summary: Update an outbound webhook without returning its secret
    security: [{BearerAuth: []}]
    parameters:
      - {name: webhook_id, in: path, type: integer, required: true}
    responses:
      200: {description: Webhook updated}
    """
    try:
        _webhook_admin_required()
        webhook = change_automation.update_webhook(
            webhook_id, body, g.user_params['group_id']
        )
        return jsonify({
            'status': 'success',
            'data': change_automation.serialize_webhook(webhook),
        })
    except Exception as exc:
        return _error_response(exc)


@bp.delete('/changes/webhooks/<int:webhook_id>')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def delete_config_change_webhook(webhook_id: int):
    """Delete a Change Center webhook.
    ---
    tags: [Configuration changes]
    summary: Delete an outbound webhook
    security: [{BearerAuth: []}]
    parameters:
      - {name: webhook_id, in: path, type: integer, required: true}
    responses:
      200: {description: Webhook deleted}
    """
    try:
        _webhook_admin_required()
        change_automation.delete_webhook(webhook_id, g.user_params['group_id'])
        return jsonify({'status': 'success'})
    except Exception as exc:
        return _error_response(exc)


@bp.post('/changes/webhooks/<int:webhook_id>/test')
@jwt_required()
@get_user_params()
@page_for_admin(level=3)
@check_group()
@change_center_subscription_required
def test_config_change_webhook(webhook_id: int):
    """Queue a test webhook delivery.
    ---
    tags: [Configuration changes]
    summary: Queue a signed test delivery for an outbound webhook
    security: [{BearerAuth: []}]
    parameters:
      - {name: webhook_id, in: path, type: integer, required: true}
    responses:
      200: {description: Test delivery queued}
    """
    try:
        _webhook_admin_required()
        webhook = change_automation.queue_webhook_test(
            webhook_id, g.user_params['group_id'], g.user_params['user_id']
        )
        return jsonify({
            'status': 'success',
            'data': change_automation.serialize_webhook(webhook),
        })
    except Exception as exc:
        return _error_response(exc)
