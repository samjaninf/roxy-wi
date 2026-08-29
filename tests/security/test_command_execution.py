import sys

import pytest

from app.modules.config import config as config_module
from app.modules.roxywi import nettools
from app.modules.server.command import (
    CommandResult,
    build_remote_command,
    build_remote_pipeline,
    run_local,
)
from app.routes.portscanner import routes as portscanner_routes


@pytest.mark.security
def test_local_executor_treats_shell_metacharacters_as_one_argument():
    payload = 'value; echo injected && $(id) | reboot'

    result = run_local([
        sys.executable,
        '-c',
        'import sys; print(sys.argv[1])',
        payload,
    ])

    assert result.succeeded
    assert result.stdout.strip() == payload


@pytest.mark.security
def test_local_executor_rejects_shell_command_strings():
    with pytest.raises(TypeError, match='sequence'):
        run_local('echo safe; echo unsafe')


@pytest.mark.security
def test_local_executor_reports_timeout_without_raising():
    result = run_local(
        [sys.executable, '-c', 'import time; time.sleep(1)'],
        timeout=0.01,
    )

    assert result.timed_out is True
    assert result.return_code == -1
    assert 'timed out' in result.stderr


@pytest.mark.security
def test_remote_command_builder_quotes_every_argument():
    command = build_remote_command(
        'docker',
        ['exec', 'nginx-prod; touch /tmp/pwned', 'nginx', '-T'],
        sudo=True,
        merge_stderr=True,
    )

    assert command == "sudo docker exec 'nginx-prod; touch /tmp/pwned' nginx -T 2>&1"


@pytest.mark.security
def test_remote_command_builder_rejects_executable_injection():
    with pytest.raises(ValueError, match='Invalid remote executable'):
        build_remote_command('nmap; reboot', ['192.0.2.10'])


@pytest.mark.security
def test_remote_pipeline_quotes_each_command_separately():
    command = build_remote_pipeline([
        ('printf', ['%s', 'exit']),
        ('nc', ['example.test; reboot', 443]),
    ])

    assert command == "printf %s exit | nc 'example.test; reboot' 443"


@pytest.mark.security
def test_portscanner_runs_nmap_once_without_a_shell(app, monkeypatch):
    calls = []
    nmap_output = (
        'Starting Nmap\n'
        'Nmap scan report for 192.0.2.10\n'
        'Host is up\n'
        'Not shown: 998 closed ports\n'
        'PORT     STATE SERVICE\n'
        '22/tcp   open  ssh\n'
        '443/tcp  open  https\n'
    )
    monkeypatch.setattr(
        portscanner_routes,
        'run_local',
        lambda args, **kwargs: calls.append((args, kwargs)) or CommandResult(
            args=tuple(args), stdout=nmap_output, stderr='', return_code=0
        ),
    )
    monkeypatch.setattr(portscanner_routes.roxywi_common, 'get_user_lang_for_flask', lambda: 'en')

    with app.test_request_context('/portscanner/scan', method='POST', json={'ip': '192.0.2.10'}):
        response = portscanner_routes.scan_port()

    assert response.get_json()['status'] == 'Ok'
    assert calls == [([
        'sudo', '-n', 'nmap', '-n', '-sS', '-T4', '--max-retries', '1',
        '--host-timeout', '40s', '192.0.2.10',
    ], {'timeout': 45})]


@pytest.mark.security
def test_portscanner_rejects_command_payload_before_execution(app, monkeypatch):
    monkeypatch.setattr(
        portscanner_routes,
        'run_local',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('nmap must not run')),
    )

    with app.test_request_context(
        '/portscanner/scan',
        method='POST',
        json={'ip': '192.0.2.10; touch /tmp/pwned'},
    ):
        response, status = portscanner_routes.scan_port()

    assert status == 400
    assert response.get_json()['error'] == 'Invalid IP address or DNS name'


@pytest.mark.security
def test_portscanner_returns_before_reverse_proxy_timeout(app, monkeypatch):
    monkeypatch.setattr(
        portscanner_routes,
        'run_local',
        lambda args, **kwargs: CommandResult(
            args=tuple(args),
            stdout='',
            stderr='Command timed out after 45 seconds',
            return_code=-1,
            timed_out=True,
        ),
    )

    with app.test_request_context('/portscanner/scan', method='POST', json={'ip': '192.0.2.10'}):
        response, status = portscanner_routes.scan_port()

    assert status == 408
    assert response.get_json() == {'error': 'Port scan exceeded 45 seconds'}


@pytest.mark.security
def test_nettools_tcp_check_uses_argument_array(monkeypatch):
    calls = []
    monkeypatch.setattr(
        nettools,
        'run_local',
        lambda args, **kwargs: calls.append((args, kwargs)) or CommandResult(
            args=tuple(str(arg) for arg in args), stdout='Connected\n', stderr='', return_code=0
        ),
    )

    response = nettools.telnet_from_server('localhost', 'example.test', 443)

    assert response == 'Connected<br>'
    assert calls == [(
        ['nc', 'example.test', 443, '-t', '-w', '1s'],
        {'input_text': 'exit\n', 'timeout': 5},
    )]


@pytest.mark.security
def test_nettools_streamed_ping_keeps_formatting_and_escapes_command_output(app, monkeypatch):
    monkeypatch.setattr(
        nettools,
        'stream_local',
        lambda args: iter([b'PING example.test <script>alert(1)</script>\n64 bytes time=1 ms\n']),
    )

    with app.test_request_context('/nettools/icmp'):
        response = nettools.ping_from_server('localhost', 'example.test', 'ping')
        output = response.get_data(as_text=True)

    assert '<span style="color: var(--link-dark-blue);' in output
    assert '<script>' not in output
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in output
    assert '<div class="ping_pre">' not in output


@pytest.mark.security
def test_nettools_dns_check_filters_output_in_python(monkeypatch):
    calls = []
    dig_output = (
        '; unrelated diagnostic\n'
        'example.test. 300 IN A 192.0.2.10 <script>alert(1)</script>\n'
        ';; SERVER: 192.0.2.53#53\n'
    )
    monkeypatch.setattr(
        nettools,
        'run_local',
        lambda args, **kwargs: calls.append((args, kwargs)) or CommandResult(
            args=tuple(str(arg) for arg in args), stdout=dig_output, stderr='', return_code=0
        ),
    )

    response = nettools.nslookup_from_server('localhost', 'example.test', 'a')

    assert 'unrelated diagnostic' not in response
    assert '192.0.2.10' in response
    assert '<script>' not in response
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response
    assert 'From NS server' in response
    assert calls == [(['dig', 'example.test', 'a'], {'timeout': 10})]


@pytest.mark.security
def test_nettools_whois_escapes_external_registry_values(monkeypatch):
    whois_payload = {
        'domain_name': '<script>alert(1)</script>',
        'registrar': 'Example & Sons',
        'creation_date': '2024-01-01',
        'expiration_date': '2027-01-01',
        'name_servers': ['ns1.example.test'],
        'status': 'active',
    }
    monkeypatch.setattr(nettools.whois, 'whois', lambda domain: __import__('json').dumps(whois_payload))

    response = nettools.whois_check('example.test')

    assert '<script>' not in response
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response
    assert 'Example &amp; Sons' in response


@pytest.mark.security
def test_config_diff_handles_shell_metacharacters_as_file_names(tmp_path, monkeypatch):
    old_config = tmp_path / 'old; echo injected.cfg'
    new_config = tmp_path / 'new && reboot.cfg'
    old_config.write_text('global\n  maxconn 1000\n', encoding='utf-8')
    new_config.write_text('global\n  maxconn 2000\n', encoding='utf-8')
    monkeypatch.setattr(
        config_module.server_mod,
        'subprocess_execute',
        lambda command: (_ for _ in ()).throw(AssertionError('config diff must not invoke a shell')),
    )

    result = config_module.diff_config(old_config, new_config)

    assert '-  maxconn 1000' in result
    assert '+  maxconn 2000' in result
    assert str(old_config) in result
    assert str(new_config) in result
