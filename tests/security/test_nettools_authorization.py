from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

import app.modules.roxywi.common as roxywi_common
import app.routes.main.routes as main_routes
from app.modules.roxywi.exception import RoxywiResourceNotFound


NETTOOLS_REQUESTS = (
    (
        'icmp',
        {'server_to': '198.51.100.10', 'action': 'ping'},
        'ping_from_server',
    ),
    (
        'tcp',
        {'server_to': '198.51.100.10', 'port': 443},
        'telnet_from_server',
    ),
    (
        'dns',
        {'dns_name': 'audit.example', 'record_type': 'a'},
        'nslookup_from_server',
    ),
)


@pytest.fixture()
def nettools_user(app, monkeypatch):
    user_params = {
        'user_id': 1,
        'user': 'group-a-guest',
        'role': 4,
        'group_id': 1,
        'lang': 'en',
        'servers': [],
        'user_services': [],
    }
    monkeypatch.setattr(
        roxywi_common,
        'get_users_params',
        lambda **_kwargs: dict(user_params),
    )
    with app.app_context():
        token = create_access_token('1', additional_claims={'group': '1'})
    return {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
    }


def _stub_nettools_operation(monkeypatch, operation_name, calls):
    def operation(*args):
        calls.append(args)
        return 'ok'

    monkeypatch.setattr(main_routes.nettools_mod, operation_name, operation)


@pytest.mark.security
@pytest.mark.parametrize(('check', 'payload', 'operation_name'), NETTOOLS_REQUESTS)
@pytest.mark.parametrize('server_from', ('192.0.2.20', '192.0.2.99'))
def test_nettools_rejects_foreign_and_unknown_execution_hosts_before_ssh(
    client,
    monkeypatch,
    nettools_user,
    check,
    payload,
    operation_name,
    server_from,
):
    calls = []
    _stub_nettools_operation(monkeypatch, operation_name, calls)

    def get_server_by_ip(server_ip):
        if server_ip == '192.0.2.20':
            return SimpleNamespace(server_id=20, ip=server_ip, group_id=2)
        raise RoxywiResourceNotFound

    monkeypatch.setattr(roxywi_common.server_sql, 'get_server_by_ip', get_server_by_ip)

    response = client.post(
        f'/nettools/{check}',
        headers=nettools_user,
        json={'server_from': server_from, **payload},
    )

    assert response.status_code == 403, response.get_data(as_text=True)
    assert response.is_json
    assert 'active group' in response.get_json()['error']
    assert calls == []


@pytest.mark.security
@pytest.mark.parametrize(('check', 'payload', 'operation_name'), NETTOOLS_REQUESTS)
def test_nettools_allows_an_execution_host_from_the_active_group(
    client,
    monkeypatch,
    nettools_user,
    check,
    payload,
    operation_name,
):
    calls = []
    lookups = []
    _stub_nettools_operation(monkeypatch, operation_name, calls)

    def get_server_by_ip(server_ip):
        lookups.append(server_ip)
        return SimpleNamespace(server_id=10, ip=server_ip, group_id=1)

    monkeypatch.setattr(roxywi_common.server_sql, 'get_server_by_ip', get_server_by_ip)

    response = client.post(
        f'/nettools/{check}',
        headers=nettools_user,
        json={'server_from': '192.0.2.10', **payload},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert lookups == ['192.0.2.10']
    assert len(calls) == 1
    assert calls[0][0] == '192.0.2.10'


@pytest.mark.security
@pytest.mark.parametrize(('check', 'payload', 'operation_name'), NETTOOLS_REQUESTS)
def test_nettools_explicitly_allows_localhost_without_an_ssh_credential_lookup(
    client,
    monkeypatch,
    nettools_user,
    check,
    payload,
    operation_name,
):
    calls = []
    _stub_nettools_operation(monkeypatch, operation_name, calls)
    monkeypatch.setattr(
        roxywi_common.server_sql,
        'get_server_by_ip',
        lambda _server_ip: pytest.fail('localhost must not use a stored SSH credential'),
    )

    response = client.post(
        f'/nettools/{check}',
        headers=nettools_user,
        json={'server_from': 'localhost', **payload},
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert len(calls) == 1
    assert calls[0][0] == 'localhost'
