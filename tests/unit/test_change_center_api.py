from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

import app.routes.change.routes as change_routes
from app.modules.roxywi.exception import (
    RoxywiConflictError,
    RoxywiResourceNotFound,
    RoxywiValidationError,
)


def _change(change_id=1, service='haproxy', group_id=1):
    return SimpleNamespace(id=change_id, service=service, group_id=group_id, server_id=10)


@pytest.fixture()
def change_api(app, monkeypatch):
    user_params = {
        'user_id': 1,
        'user': 'admin',
        'role': 1,
        'group_id': 1,
        'lang': 'en',
    }
    monkeypatch.setattr(
        change_routes.roxywi_common,
        'get_users_params',
        lambda **_kwargs: dict(user_params),
    )
    monkeypatch.setattr(
        change_routes.roxywi_common,
        'return_user_subscription',
        lambda: {'user_status': 1, 'user_plan': 'support'},
    )
    monkeypatch.setattr(change_routes.roxywi_auth, 'is_admin', lambda **_kwargs: True)
    monkeypatch.setattr(
        change_routes.roxywi_auth,
        'is_access_permit_to_service',
        lambda _service: True,
    )
    monkeypatch.setattr(change_routes.roxywi_common, 'logging', lambda *_args, **_kwargs: None)
    with app.app_context():
        token = create_access_token('1', additional_claims={'group': '1'})
    return user_params, {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}


@pytest.mark.parametrize(
    ('user_status', 'plan'),
    ((1, 'user'), (1, 'company'), (1, 'cloud'), (1, 'Trial'), (0, 'support')),
)
def test_change_center_shows_upgrade_page_but_internal_api_rejects_non_premium_plans(
    client, monkeypatch, change_api, user_status, plan
):
    _user_params, headers = change_api
    monkeypatch.setattr(
        change_routes.roxywi_common,
        'return_user_subscription',
        lambda: {'user_status': user_status, 'user_plan': plan},
    )
    monkeypatch.setattr(
        change_routes,
        'render_template',
        lambda template, **_kwargs: template,
    )

    page_response = client.get('/changes', headers=headers)
    api_response = client.get('/changes/api', headers=headers)

    assert page_response.status_code == 200
    assert page_response.get_data(as_text=True) == 'change_center.html'
    assert api_response.status_code == 403
    assert api_response.get_json()['error'] == 'Change Center requires an active Premium plan'


def test_change_api_lists_only_permitted_services(client, monkeypatch, change_api):
    _user_params, headers = change_api
    changes = [_change(1, 'haproxy'), _change(2, 'nginx')]
    monkeypatch.setattr(change_routes.change_sql, 'list_changes', lambda *_args, **_kwargs: changes)
    monkeypatch.setattr(
        change_routes.roxywi_auth,
        'is_access_permit_to_service',
        lambda service: service == 'haproxy',
    )
    monkeypatch.setattr(
        change_routes.change_service,
        'serialize_change',
        lambda change: {'id': change.id, 'service': change.service},
    )

    response = client.get('/changes/api?service=haproxy&status=validated', headers=headers)

    assert response.status_code == 200
    assert response.get_json()['data'] == [{'id': 1, 'service': 'haproxy'}]


def test_change_api_rejects_unsupported_service_filter(client, change_api):
    _user_params, headers = change_api

    response = client.get('/changes/api?service=ssh', headers=headers)

    assert response.status_code == 400
    assert response.get_json()['error'] == 'Unsupported service filter'


def test_change_api_creates_and_returns_change(client, monkeypatch, change_api):
    _user_params, headers = change_api
    created = _change(7)
    monkeypatch.setattr(change_routes.change_service, 'create_change', lambda *_args: created)
    monkeypatch.setattr(
        change_routes.change_service,
        'serialize_change',
        lambda change: {'id': change.id, 'service': change.service},
    )

    response = client.post(
        '/changes/api',
        headers=headers,
        json={
            'server_id': 10,
            'service': 'haproxy',
            'action': 'reload',
            'config': 'global\n',
            'title': 'API change',
        },
    )

    assert response.status_code == 201
    assert response.get_json()['data']['id'] == 7


def test_change_api_rejects_creation_without_service_access(client, monkeypatch, change_api):
    _user_params, headers = change_api
    monkeypatch.setattr(
        change_routes.roxywi_auth,
        'is_access_permit_to_service',
        lambda _service: False,
    )

    response = client.post(
        '/changes/api',
        headers=headers,
        json={
            'server_id': 10,
            'service': 'haproxy',
            'config': 'global\n',
            'title': 'Forbidden change',
        },
    )

    assert response.status_code == 403
    assert 'No access' in response.get_json()['error']


def test_change_api_returns_not_found_for_unknown_change(client, monkeypatch, change_api):
    _user_params, headers = change_api
    monkeypatch.setattr(
        change_routes.change_sql,
        'get_change',
        lambda _change_id: (_ for _ in ()).throw(RoxywiResourceNotFound()),
    )

    response = client.get('/changes/api/999', headers=headers)

    assert response.status_code == 404
    assert response.get_json()['status'] == 'failed'


@pytest.mark.parametrize(('group_id', 'service_allowed'), ((2, True), (1, False)))
def test_change_api_protects_group_and_service_access(
    client, monkeypatch, change_api, group_id, service_allowed
):
    _user_params, headers = change_api
    monkeypatch.setattr(
        change_routes.change_sql,
        'get_change',
        lambda _change_id: _change(5, group_id=group_id),
    )
    monkeypatch.setattr(
        change_routes.roxywi_auth,
        'is_access_permit_to_service',
        lambda _service: service_allowed,
    )

    response = client.get('/changes/api/5', headers=headers)

    assert response.status_code == 403


def test_change_api_updates_draft(client, monkeypatch, change_api):
    _user_params, headers = change_api
    change = _change(6)
    monkeypatch.setattr(change_routes.change_sql, 'get_change', lambda _change_id: change)
    monkeypatch.setattr(change_routes.change_service, 'update_change', lambda *_args: change)
    monkeypatch.setattr(
        change_routes.change_service,
        'serialize_change',
        lambda item: {'id': item.id},
    )

    response = client.put('/changes/api/6', headers=headers, json={'title': 'Updated'})

    assert response.status_code == 200
    assert response.get_json()['data']['id'] == 6


def test_change_api_maps_update_conflict(client, monkeypatch, change_api):
    _user_params, headers = change_api
    change = _change(6)
    monkeypatch.setattr(change_routes.change_sql, 'get_change', lambda _change_id: change)
    monkeypatch.setattr(
        change_routes.change_service,
        'update_change',
        lambda *_args: (_ for _ in ()).throw(RoxywiConflictError('Already deployed')),
    )

    response = client.put('/changes/api/6', headers=headers, json={'title': 'Updated'})

    assert response.status_code == 409
    assert response.get_json()['error'] == 'Already deployed'


@pytest.mark.parametrize('action', ('validate', 'deploy', 'rollback', 'cancel', 'recover'))
def test_change_api_exposes_workflow_actions(client, monkeypatch, change_api, action):
    _user_params, headers = change_api
    change = _change(8)
    monkeypatch.setattr(change_routes.change_sql, 'get_change', lambda _change_id: change)
    monkeypatch.setattr(
        change_routes.change_service,
        f'{action}_change',
        lambda *_args: change,
    )
    monkeypatch.setattr(
        change_routes.change_service,
        'serialize_change',
        lambda item: {'id': item.id},
    )

    response = client.post(f'/changes/api/{change.id}/{action}', headers=headers)

    assert response.status_code == 200
    assert response.get_json()['data']['id'] == change.id


def test_change_api_requires_admin_for_approval(client, monkeypatch, change_api):
    user_params, headers = change_api
    user_params['role'] = 3

    response = client.post('/changes/api/1/approve', headers=headers)

    assert response.status_code == 403


def test_change_api_allows_distinct_admin_to_approve(client, monkeypatch, change_api):
    _user_params, headers = change_api
    change = _change(11)
    monkeypatch.setattr(change_routes.change_sql, 'get_change', lambda _change_id: change)
    monkeypatch.setattr(change_routes.change_service, 'approve_change', lambda *_args: change)
    monkeypatch.setattr(
        change_routes.change_service,
        'serialize_change',
        lambda item: {'id': item.id},
    )

    response = client.post('/changes/api/11/approve', headers=headers)

    assert response.status_code == 200
    assert response.get_json()['data']['id'] == 11


def test_change_api_maps_workflow_validation_error(client, monkeypatch, change_api):
    _user_params, headers = change_api
    change = _change(9)
    monkeypatch.setattr(change_routes.change_sql, 'get_change', lambda _change_id: change)
    monkeypatch.setattr(
        change_routes.change_service,
        'deploy_change',
        lambda *_args: (_ for _ in ()).throw(RoxywiValidationError('Deployment check failed')),
    )

    response = client.post('/changes/api/9/deploy', headers=headers)

    assert response.status_code == 400
    assert response.get_json()['error'] == 'Deployment check failed'


def test_change_api_hides_unexpected_exception_behind_failed_response(
    client, monkeypatch, change_api
):
    _user_params, headers = change_api
    monkeypatch.setattr(
        change_routes.change_sql,
        'list_changes',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('database unavailable')),
    )

    response = client.get('/changes/api', headers=headers)

    assert response.status_code == 500
    assert response.get_json() == {
        'status': 'failed',
        'error': 'Internal server error',
    }
