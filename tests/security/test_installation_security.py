import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import g

from app.modules.db.db_model import InstallationTasks
from app.modules.roxywi.class_models import HAClusterRequest, HAClusterService, ServerInstall, ServiceInstall
from app.modules.roxywi.exception import RoxywiGroupMismatch
from app.modules.service import installation
from app.views.ha.views import HAView


def _service_install(*server_ids: int) -> ServiceInstall:
    return ServiceInstall(
        servers=[ServerInstall(id=server_id, master=False) for server_id in server_ids],
        services={'haproxy': HAClusterService(enabled=True, docker=False)},
        checker=False,
        metrics=False,
        auto_start=False,
        syn_flood=False,
        docker=False,
    )


@pytest.mark.security
def test_installation_authorizes_every_server_from_request_body(app, monkeypatch):
    servers = {
        10: SimpleNamespace(server_id=10, ip='192.0.2.10', group_id=7),
        20: SimpleNamespace(server_id=20, ip='198.51.100.20', group_id=9),
    }
    generator_called = False

    def generate_inventory(*args, **kwargs):
        nonlocal generator_called
        generator_called = True
        return {}, []

    monkeypatch.setattr(installation.server_sql, 'get_server', lambda server_id: servers[server_id])
    monkeypatch.setattr(installation, 'generate_haproxy_inv', generate_inventory)

    with app.test_request_context('/install/haproxy/10'):
        g.user_params = {'group_id': 7, 'role': 2}
        with pytest.raises(RoxywiGroupMismatch):
            installation.install_service('haproxy', _service_install(10, 20))

    assert generator_called is False


@pytest.mark.security
def test_service_is_activated_only_from_success_callback(app, monkeypatch):
    captured = {}
    activated = []

    monkeypatch.setattr(
        installation.server_sql,
        'get_server',
        lambda server_id: SimpleNamespace(server_id=server_id, ip='192.0.2.10', group_id=7),
    )
    monkeypatch.setattr(
        installation,
        'generate_haproxy_inv',
        lambda json_data, service: ({'server': {'hosts': {'192.0.2.10': {}}}}, ['192.0.2.10']),
    )
    monkeypatch.setattr(
        installation,
        'service_actions_after_install',
        lambda server_ips, service, json_data: activated.append((server_ips, service)),
    )

    def start_task(inv, server_ips, ansible_role, service_name, on_success=None):
        captured['on_success'] = on_success
        return 123

    monkeypatch.setattr(installation, 'run_ansible_thread', start_task)

    with app.test_request_context('/install/haproxy/10'):
        g.user_params = {'group_id': 7, 'role': 2}
        assert installation.install_service('haproxy', _service_install(10)) == 123

    assert activated == []
    captured['on_success']()
    assert activated == [(['192.0.2.10'], 'haproxy')]


@pytest.mark.security
def test_failed_installation_task_is_not_overwritten_as_completed(monkeypatch):
    task = InstallationTasks.create(service_name='Failed test installation', server_ids=[])
    callback_called = False

    def success_callback():
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(
        installation,
        'run_ansible',
        lambda inv, server_ips, service: {'failures': {'192.0.2.10': 1}, 'dark': {}},
    )
    monkeypatch.setattr(installation.roxywi_common, 'logging', lambda *args, **kwargs: None)

    try:
        installation.run_installations({}, ['192.0.2.10'], 'haproxy', task.id, success_callback)
        stored_task = InstallationTasks.get_by_id(task.id)
        assert stored_task.status == 'failed'
        assert 'Cannot install haproxy' in stored_task.error
        assert callback_called is False
    finally:
        task.delete_instance()


@pytest.mark.security
def test_successful_installation_activates_service_before_completing_task(monkeypatch):
    task = InstallationTasks.create(service_name='Successful test installation', server_ids=[])
    statuses_during_callback = []

    monkeypatch.setattr(
        installation,
        'run_ansible',
        lambda inv, server_ips, service: {'failures': {}, 'dark': {}},
    )

    def success_callback():
        statuses_during_callback.append(InstallationTasks.get_by_id(task.id).status)

    try:
        installation.run_installations({}, ['192.0.2.10'], 'haproxy', task.id, success_callback)
        stored_task = InstallationTasks.get_by_id(task.id)
        assert statuses_during_callback == ['running']
        assert stored_task.status == 'completed'
        assert stored_task.error is None
    finally:
        task.delete_instance()


@pytest.mark.security
def test_ansible_inventory_is_private_unique_and_removed_after_runner_error(tmp_path, monkeypatch):
    private_data_dir = tmp_path / 'ansible'
    inventory_dir = private_data_dir / 'inventory'
    observed = {}
    stopped_agents = []

    monkeypatch.setattr(installation, 'ANSIBLE_PRIVATE_DATA_DIR', str(private_data_dir))
    monkeypatch.setattr(installation, 'ANSIBLE_INVENTORY_DIR', str(inventory_dir))
    monkeypatch.setattr(installation, '_install_ansible_collections', lambda: None)
    monkeypatch.setattr(installation.sql, 'get_setting', lambda setting: None)
    monkeypatch.setattr(
        installation,
        'return_ssh_keys_path',
        lambda server_ip: {
            'enabled': False,
            'key': '',
            'password': 'temporary-secret',
            'user': 'deploy',
            'port': 22,
        },
    )
    monkeypatch.setattr(
        installation.server_mod,
        'start_ssh_agent',
        lambda: {'pid': 100, 'socket': '/tmp/test-agent.sock'},
    )
    monkeypatch.setattr(
        installation.server_mod,
        'stop_ssh_agent',
        lambda agent: stopped_agents.append(agent),
    )

    class FailingRunner:
        @staticmethod
        def run(**kwargs):
            inventory_path = Path(kwargs['inventory'])
            observed['path'] = inventory_path
            observed['data'] = json.loads(inventory_path.read_text(encoding='utf-8'))
            observed['mode'] = stat.S_IMODE(inventory_path.stat().st_mode)
            raise RuntimeError('runner failed')

    monkeypatch.setattr(installation, '_ansible_runner', lambda: FailingRunner)
    inventory = {'server': {'hosts': {'192.0.2.10': {'DOCKER': False}}}}

    with pytest.raises(RuntimeError, match='runner failed'):
        installation.run_ansible(inventory, ['192.0.2.10'], 'haproxy')

    assert observed['data']['server']['hosts']['192.0.2.10']['ansible_password'] == 'temporary-secret'
    if os.name == 'posix':
        assert observed['mode'] == 0o600
        assert stat.S_IMODE(inventory_dir.stat().st_mode) == 0o700
    assert observed['path'].name.startswith('roxywi-inventory-')
    assert not observed['path'].exists()
    assert list(inventory_dir.iterdir()) == []
    assert stopped_agents == [{'pid': 100, 'socket': '/tmp/test-agent.sock'}]


@pytest.mark.security
def test_secure_inventory_uses_a_unique_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, 'ANSIBLE_INVENTORY_DIR', str(tmp_path))
    first = installation._create_secure_inventory({'server': {'hosts': {}}})
    second = installation._create_secure_inventory({'server': {'hosts': {}}})

    try:
        assert first != second
    finally:
        installation._remove_inventory(first)
        installation._remove_inventory(second)


@pytest.mark.security
def test_inventory_cleanup_refuses_paths_outside_inventory_directory(tmp_path, monkeypatch):
    inventory_directory = tmp_path / 'inventory'
    inventory_directory.mkdir()
    outside_inventory = tmp_path / 'roxywi-inventory-outside.json'
    outside_inventory.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(installation, 'ANSIBLE_INVENTORY_DIR', str(inventory_directory))
    monkeypatch.setattr(installation.roxywi_common, 'logging', lambda *_args, **_kwargs: None)

    installation._remove_inventory(str(outside_inventory))

    assert outside_inventory.exists()


@pytest.mark.security
def test_ansible_playbook_rejects_unapproved_role_names():
    assert installation._ansible_playbook('haproxy').endswith('/roles/haproxy.yml')
    with pytest.raises(ValueError, match='Unsupported Ansible role'):
        installation._ansible_playbook('../../tmp/attacker')


def test_ha_cluster_installation_returns_flat_task_ids(app, monkeypatch):
    task_ids = iter((101, 102))
    calls = []

    def install_service(service, body, cluster_id=None):
        calls.append((service, cluster_id))
        return next(task_ids)

    monkeypatch.setattr('app.views.ha.views.service_mod.install_service', install_service)
    body = HAClusterRequest(
        name='test-cluster',
        return_master=True,
        syn_flood=True,
        use_src=True,
        virt_server=True,
        reconfigure=False,
        services={
            'haproxy': HAClusterService(enabled=True, docker=False),
            'nginx': HAClusterService(enabled=False, docker=False),
        },
    )

    with app.test_request_context('/ha/cluster/42'):
        response, status = HAView._install_service(body, 42)

    assert status == 202
    assert response.get_json() == {'status': 'accepted', 'tasks_ids': [101, 102]}
    assert calls == [('keepalived', 42), ('haproxy', None)]
