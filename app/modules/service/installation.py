import os
import json
import subprocess
import tempfile
import threading
from datetime import datetime
from functools import partial
from typing import Callable, Union, Literal
from packaging import version
from urllib.parse import urlparse

import ansible
from werkzeug.utils import secure_filename

import app.modules.db.sql as sql
import app.modules.db.add as add_sql
import app.modules.db.ha_cluster as ha_sql
import app.modules.db.server as server_sql
import app.modules.db.service as service_sql
import app.modules.service.udp as udp_mod
import app.modules.service.common as service_common
import app.modules.common.common as common
import app.modules.server.server as server_mod
import app.modules.roxywi.common as roxywi_common
from app.modules.server.ssh import return_ssh_keys_path
from app.modules.db.db_model import InstallationTasks
from app.modules.roxywi.class_models import ServiceInstall, HAClusterRequest, HaproxyGlobalRequest, \
	HaproxyDefaultsRequest, HaproxyConfigRequest


ANSIBLE_PRIVATE_DATA_DIR = '/var/www/haproxy-wi/app/scripts/ansible'
ANSIBLE_INVENTORY_DIR = f'{ANSIBLE_PRIVATE_DATA_DIR}/inventory'
_ANSIBLE_ROLE_NAMES = (
	'apache', 'apache_exporter', 'backup', 'git_backup', 'haproxy',
	'haproxy_exporter', 'haproxy_geoip', 'haproxy_section', 'keepalived',
	'keepalived_exporter', 'letsencrypt', 'letsencrypt_standalone', 'nginx',
	'nginx_exporter', 'nginx_geoip', 'nginx_section', 'node_exporter',
	's3_backup', 'smon_agent', 'udp', 'waf_haproxy', 'waf_nginx',
)
_ANSIBLE_PLAYBOOKS = {
	role: f'{ANSIBLE_PRIVATE_DATA_DIR}/roles/{role}.yml'
	for role in _ANSIBLE_ROLE_NAMES
}


def _ansible_runner():
	# ansible-runner imports Linux-only modules. Keep route imports and unit
	# tests platform-neutral; load it only when an installation is executed.
	import ansible_runner

	return ansible_runner


def _authorize_installation_servers(json_data: dict) -> None:
	"""Authorize every server supplied in an installation request body."""
	for requested_server in json_data.get('servers') or []:
		server = server_sql.get_server(requested_server['id'])
		roxywi_common.require_active_group_access(server.group_id)


def _ansible_playbook(ansible_role: str) -> str:
	try:
		return _ANSIBLE_PLAYBOOKS[ansible_role]
	except (KeyError, TypeError) as exc:
		raise ValueError('Unsupported Ansible role') from exc


def _create_secure_inventory(inv: dict) -> str:
	"""Write an Ansible inventory to a unique owner-only temporary file."""
	os.makedirs(ANSIBLE_INVENTORY_DIR, mode=0o700, exist_ok=True)
	if os.name == 'posix':
		os.chmod(ANSIBLE_INVENTORY_DIR, 0o700)

	file_descriptor, inventory = tempfile.mkstemp(
		prefix='roxywi-inventory-', suffix='.json', dir=ANSIBLE_INVENTORY_DIR, text=True
	)
	try:
		with os.fdopen(file_descriptor, 'w', encoding='utf-8') as inventory_file:
			json.dump(inv, inventory_file)
		if os.name == 'posix':
			os.chmod(inventory, 0o600)
	except Exception:
		try:
			os.close(file_descriptor)
		except OSError:
			pass
		try:
			os.remove(inventory)
		except OSError:
			pass
		raise

	return inventory


def _remove_inventory(inventory: str) -> None:
	if not inventory:
		return
	inventory_root = os.path.realpath(ANSIBLE_INVENTORY_DIR)
	inventory_name = os.path.basename(inventory)
	safe_name = secure_filename(inventory_name)
	if (
		not safe_name
		or safe_name != inventory_name
		or not safe_name.startswith('roxywi-inventory-')
		or not safe_name.endswith('.json')
	):
		roxywi_common.logging('Roxy-WI server', 'error: Refusing to remove an invalid Ansible inventory path')
		return
	safe_inventory = os.path.realpath(os.path.join(inventory_root, safe_name))
	if os.path.dirname(safe_inventory) != inventory_root or os.path.realpath(inventory) != safe_inventory:
		roxywi_common.logging('Roxy-WI server', 'error: Refusing to remove an Ansible inventory outside its directory')
		return
	if os.path.exists(safe_inventory):
		try:
			os.remove(safe_inventory)
		except OSError as error:
			roxywi_common.logging('Roxy-WI server', f'error: Cannot remove temporary Ansible inventory: {error}')


def generate_udp_inv(listener_id: int, action: str) -> object:
	inv = {"server": {"hosts": {}}}
	server_ips = []
	listener = udp_mod.get_listener_config(listener_id)
	if listener['cluster_id']:
		server_ips = udp_mod.get_slaves_for_udp_listener(listener['cluster_id'], listener['vip'])
	elif listener['server_id']:
		server = server_sql.get_server(listener['server_id'])
		server_ips.append(server.ip)
	for server_ip in server_ips:
		inv['server']['hosts'][server_ip] = {
			'action': action,
			"vip": listener['vip'],
			"port": listener['port'],
			"id": listener['id'],
			"config": listener['config'],
			"lb_algo": listener['lb_algo'],
			"check_enabled": listener['check_enabled'],
			"delay_before_retry": listener['delay_before_retry'],
			"delay_loop": listener['delay_loop'],
			"retry": listener['retry'],
		}
	return inv, server_ips


def generate_geoip_inv(server_ip: str, installed_service: str, geoip_update: int) -> object:
	inv = {"server": {"hosts": {}}}
	server_ips = []

	inv['server']['hosts'][server_ip] = {
		'service_dir': common.return_nice_path(sql.get_setting(f'{installed_service}_dir')),
		'maxmind_key': sql.get_setting('maxmind_key'),
		'UPDATE': geoip_update

	}
	server_ips.append(server_ip)

	return inv, server_ips


def generate_kp_inv(json_data: json, installed_service) -> object:
	inv = {"server": {"hosts": {}}}
	server_ips = []
	cluster_id = int(json_data['cluster_id'])
	haproxy = json_data['services']['haproxy']['enabled']
	nginx = json_data['services']['nginx']['enabled']
	apache = json_data['services']['apache']['enabled']
	keepalived_path_logs = sql.get_setting('keepalived_path_logs')
	syn_flood_protect = str(json_data['syn_flood'])
	routers = {}
	vips = ha_sql.select_cluster_vips(cluster_id)

	for vip in vips:
		router_id = str(vip.router_id)
		routers[router_id] = {}
		routers[router_id].setdefault('return_master', vip.return_master)
		routers[router_id].setdefault('vip', vip.vip)
		routers[router_id].setdefault('use_src', vip.use_src)
		slaves = ha_sql.select_cluster_slaves_for_inv(router_id)
		for slave in slaves:
			slave_ip = slave.server_id.ip
			routers[router_id].setdefault(slave_ip, dict())
			routers[router_id][slave_ip].setdefault('master', slave.master)
			routers[router_id][slave_ip].setdefault('eth', slave.eth)

	for v in json_data['servers']:
		s = server_sql.get_server(v['id'])
		inv['server']['hosts'][s.ip] = {
			"HAPROXY": haproxy,
			"NGINX": nginx,
			"APACHE": apache,
			"RESTART": 1,
			"SYN_FLOOD": syn_flood_protect,
			"keepalived_path_logs": keepalived_path_logs,
			"routers": routers
		}
		server_ips.append(s.ip)

	return inv, server_ips


def generate_waf_inv(server_ip: str, installed_service: str) -> object:
	inv = {"server": {"hosts": {}}}
	server_ips = []

	inv['server']['hosts'][server_ip] = {
		'SERVICE_PATH': common.return_nice_path(sql.get_setting(f'{installed_service}_dir'))
	}
	server_ips.append(server_ip)

	return inv, server_ips


def generate_haproxy_inv(json_data: ServiceInstall, installed_service: str) -> object:
	inv = {"server": {"hosts": {}}}
	slaves = []
	server_ips = []
	master_ip = 0
	hap_sock_p = str(sql.get_setting('haproxy_sock_port'))
	stats_port = str(sql.get_setting('haproxy_stats_port'))
	server_state_file = sql.get_setting('server_state_file')
	stats_user = sql.get_setting('haproxy_stats_user')
	stats_password = sql.get_setting('haproxy_stats_password')
	haproxy_dir = sql.get_setting('haproxy_dir')
	container_name = sql.get_setting('haproxy_container_name')
	haproxy_ver = json_data['servers'][0]['version']
	is_docker = json_data['services']['haproxy']['docker']

	for v in json_data['servers']:
		s = server_sql.get_server(v['id'])
		if not v['master']:
			slaves.append(s.ip)
		else:
			master_ip = s.ip

		if 'version' in v:
			haproxy_ver = v['version']

		inv['server']['hosts'][s.ip] = {
			"SOCK_PORT": hap_sock_p,
			"STAT_PORT": stats_port,
			"STAT_FILE": server_state_file,
			"STATS_USER": stats_user,
			"CONT_NAME": container_name,
			"HAP_DIR": haproxy_dir,
			"STATS_PASS": stats_password,
			"HAPVER": haproxy_ver,
			"SYN_FLOOD": '0',
			"M_OR_S": v['master'],
			"MASTER": master_ip,
			"slaves": slaves,
			"DOCKER": is_docker
		}
		server_ips.append(s.ip)

	return inv, server_ips


def generate_section_inv(json_data: dict, cfg: str, service: Literal['haproxy', 'nginx']) -> dict:
	cert_path = sql.get_setting('cert_path')
	service_dir = sql.get_setting(f'{service}_dir')
	if 'external-check command' in json_data:
		raise Exception('External check command is not supported for HAProxy')
	inv = {"server": {"hosts": {}}}
	inv['server']['hosts']['localhost'] = {
		"config": json_data,
		"cert_path": cert_path,
		"service_dir": service_dir,
		"cfg": cfg,
		"action": 'create'
	}
	return inv


def generate_section_inv_for_del(cfg: str, section_type: str, section_name: str) -> dict:
	config = {'type': section_type, 'name': section_name}
	inv = {"server": {"hosts": {}}}
	inv['server']['hosts']['localhost'] = {
		"config": config,
		"cfg": cfg,
		"action": 'delete'
	}

	return inv


def generate_service_inv(json_data: ServiceInstall, installed_service: str) -> object:
	inv = {"server": {"hosts": {}}}
	server_ips = []
	stats_user = sql.get_setting(f'{installed_service}_stats_user')
	stats_password = sql.get_setting(f'{installed_service}_stats_password')
	stats_port = str(sql.get_setting(f'{installed_service}_stats_port'))
	stats_page = sql.get_setting(f'{installed_service}_stats_page')
	config_path = sql.get_setting(f'{installed_service}_config_path')
	service_dir = sql.get_setting(f'{installed_service}_dir')
	container_name = sql.get_setting(f'{installed_service}_container_name')
	is_docker = json_data['services'][installed_service]['docker']

	if installed_service == 'nginx' and not os.path.isdir('/var/www/haproxy-wi/app/scripts/ansible/roles/nginxinc.nginx'):
		result = subprocess.run(
			[
				'ansible-galaxy', 'install', 'nginxinc.nginx,0.24.3', '-f',
				'--roles-path', '/var/www/haproxy-wi/app/scripts/ansible/roles/'
			],
			check=False,
		)
		if result.returncode != 0:
			raise RuntimeError('Cannot install the nginxinc.nginx Ansible role')

	for v in json_data['servers']:
		s = server_sql.get_server(v['id'])
		if installed_service == 'apache':
			correct_service_name = service_common.get_correct_apache_service_name(server_id=v['id'])
			if service_dir == '/etc/httpd' and correct_service_name == 'apache2':
				service_dir = '/etc/apache2'
			elif service_dir == '/etc/apache2' and correct_service_name == 'httpd':
				service_dir = '/etc/httpd'

		inv['server']['hosts'][s.ip] = {
			"STAT_PORT": stats_port,
			"DOCKER": is_docker,
			"STATS_USER": stats_user,
			"CONT_NAME": container_name,
			"STATS_PASS": stats_password,
			"service_dir": service_dir,
			"SYN_FLOOD": "0",
			"CONFIG_PATH": config_path,
			"STAT_PAGE": stats_page,
			"service": installed_service,
		}
		server_ips.append(s.ip)

	return inv, server_ips


def run_ansible(inv: dict, server_ips: list, ansible_role: str) -> dict:
	proxy = sql.get_setting('proxy')
	proxy_serv = proxy if proxy not in (None, '', 'None') else ''
	tags = ''
	agent_pid = None
	inventory = ''

	playbook = _ansible_playbook(ansible_role)
	try:
		try:
			agent_pid = server_mod.start_ssh_agent()
		except Exception as error:
			raise RuntimeError(f'Cannot start SSH agent: {error}') from error

		_install_ansible_collections()

		for server_ip in server_ips:
			if server_ip != 'localhost':
				ssh_settings = return_ssh_keys_path(server_ip)
				if ssh_settings['enabled']:
					inv['server']['hosts'][server_ip]['ansible_ssh_private_key_file'] = ssh_settings['key']
				inv['server']['hosts'][server_ip]['ansible_password'] = ssh_settings['password']
				inv['server']['hosts'][server_ip]['ansible_user'] = ssh_settings['user']
				inv['server']['hosts'][server_ip]['ansible_port'] = ssh_settings['port']
				inv['server']['hosts'][server_ip]['ansible_become'] = True

				if ssh_settings['enabled']:
					server_mod.add_key_to_agent(ssh_settings, agent_pid)

			inv['server']['hosts'][server_ip]['PROXY'] = proxy_serv

			if 'DOCKER' in inv['server']['hosts'][server_ip]:
				tags = 'docker' if inv['server']['hosts'][server_ip]['DOCKER'] else 'system'

		inventory = _create_secure_inventory(inv)
		envvars = {
			'ANSIBLE_DISPLAY_OK_HOSTS': 'no',
			'ANSIBLE_SHOW_CUSTOM_STATS': 'no',
			'ANSIBLE_DISPLAY_SKIPPED_HOSTS': 'no',
			'ANSIBLE_DEPRECATION_WARNINGS': 'no',
			'ANSIBLE_HOST_KEY_CHECKING': 'yes',
			'ACTION_WARNINGS': 'no',
			'LOCALHOST_WARNING': 'no',
			'COMMAND_WARNINGS': 'no',
			'AWX_DISPLAY': False,
			'SSH_AUTH_PID': agent_pid['pid'],
			'SSH_AUTH_SOCK': agent_pid['socket'],
			'ANSIBLE_PYTHON_INTERPRETER': '/usr/bin/python3'
		}
		result = _ansible_runner().run(
			private_data_dir=ANSIBLE_PRIVATE_DATA_DIR,
			inventory=inventory,
			envvars=envvars,
			playbook=playbook,
			tags=tags,
		)
		if result.rc != 0:
			raise RuntimeError(
				'Something wrong with installation, check '
				'<a href="/logs/internal?log_file=roxy-wi.error.log" target="_blank" class="link">'
				'Apache logs</a> for details'
			)
		return result.stats
	finally:
		_remove_inventory(inventory)
		if agent_pid is not None:
			try:
				server_mod.stop_ssh_agent(agent_pid)
			except Exception as error:
				roxywi_common.logging('Roxy-WI server', f'error: Cannot stop SSH agent {error}')


def run_ansible_locally(inv: dict, ansible_role: str) -> dict:
	proxy = sql.get_setting('proxy')
	inv['server']['hosts']['localhost']['PROXY'] = proxy if proxy not in (None, '', 'None') else ''

	envvars = {
		'ANSIBLE_DISPLAY_OK_HOSTS': 'no',
		'ANSIBLE_SHOW_CUSTOM_STATS': 'no',
		'ANSIBLE_DISPLAY_SKIPPED_HOSTS': "no",
		'ANSIBLE_DEPRECATION_WARNINGS': "no",
		'ANSIBLE_HOST_KEY_CHECKING': "yes",
		'ACTION_WARNINGS': "no",
		'LOCALHOST_WARNING': "no",
		'COMMAND_WARNINGS': "no",
		'AWX_DISPLAY': False,
		'ANSIBLE_PYTHON_INTERPRETER': '/usr/bin/python3'
	}
	playbook = _ansible_playbook(ansible_role)
	inventory = ''
	try:
		inventory = _create_secure_inventory(inv)
		result = _ansible_runner().run(
			private_data_dir=ANSIBLE_PRIVATE_DATA_DIR,
			inventory=inventory,
			envvars=envvars,
			playbook=playbook,
		)
		if result.rc != 0:
			raise RuntimeError(
				'Something wrong with installation, check '
				'<a href="/logs/internal?log_file=roxy-wi.error.log" target="_blank" class="link">'
				'Apache logs</a> for details'
			)
		return result.stats
	finally:
		_remove_inventory(inventory)


def service_actions_after_install(server_ips: list[str], service: str, json_data: dict) -> None:
	update_functions = {
		'haproxy': service_sql.update_haproxy,
		'nginx': service_sql.update_nginx,
		'apache': service_sql.update_apache,
		'keepalived': service_sql.update_keepalived,
	}

	for server_ip in server_ips:
		server_id = server_sql.get_server_by_ip(server_ip).server_id
		try:
			update_functions[service](server_ip)
		except Exception as e:
			roxywi_common.handle_exceptions(e, 'Roxy-WI server', f'Cannot activate {service} on server {server_ip}')
		if service != 'keepalived':
			is_docker = json_data['services'][service]['docker']
			service_sql.insert_or_update_service_setting(server_id, service, 'restart', '1')
			if is_docker:
				service_sql.insert_or_update_service_setting(server_id, service, 'dockerized', '1')
			else:
				service_sql.insert_or_update_service_setting(server_id, service, 'dockerized', '0')
		if service == 'haproxy':
			try:
				_create_default_config_in_db(server_id)
			except Exception:
				pass


def _create_default_config_in_db(server_id: int) -> None:
	hap_sock_p = sql.get_setting('haproxy_sock_port')
	stats_port = sql.get_setting('haproxy_stats_port')
	stats_user = sql.get_setting('haproxy_stats_user')
	stats_password = sql.get_setting('haproxy_stats_password')
	config = HaproxyGlobalRequest(
		socket=[f'*:{hap_sock_p} level admin', '/var/run/haproxy.sock mode 600 level admin', '/var/lib/haproxy/stats']
	)
	add_sql.insert_or_update_new_section(server_id, 'global', 'global', config)
	add_sql.insert_or_update_new_section(server_id, 'defaults', 'defaults', HaproxyDefaultsRequest())
	option = (
		'http-request use-service prometheus-exporter if { path /metrics }\r\nstats enable\r\nstats uri /stats\r\n'
		f'stats realm HAProxy-04\ Statistics\r\nstats auth {stats_user}:{stats_password}\r\nstats admin if TRUE'
	)
	stats_config = HaproxyConfigRequest(
		binds=[{'ip': '', 'port': stats_port}],
		option=option,
		type='listen',
		name='stats',
	)
	add_sql.insert_new_section(server_id, 'listen', 'stats', stats_config)


def install_service(service: str, json_data: Union[str, ServiceInstall, HAClusterRequest], cluster_id: int = None) -> int:
	generate_functions = {
		'haproxy': generate_haproxy_inv,
		'nginx': generate_service_inv,
		'apache': generate_service_inv,
		'keepalived': generate_kp_inv,
	}

	json_data = json_data.model_dump(mode='json')
	if cluster_id:
		json_data['cluster_id'] = cluster_id
	_authorize_installation_servers(json_data)
	try:
		inv, server_ips = generate_functions[service](json_data, service)
	except Exception as e:
		raise Exception(f'Cannot generate inv {service}: {e}')
	try:
		on_success = partial(service_actions_after_install, server_ips, service, json_data)
		return run_ansible_thread(inv, server_ips, service, service.title(), on_success=on_success)
	except Exception as e:
		raise Exception(f'Cannot install {service}: {e}')


def _install_ansible_collections():
	collections = ('community.general', 'ansible.posix', 'community.docker', 'community.grafana', 'ansible.netcommon', 'ansible.utils')
	trouble_link = 'Read <a href="https://roxy-wi.org/troubleshooting#ansible_collection" target="_blank" class="link">troubleshooting</a>'
	proxy = sql.get_setting('proxy')
	environment = os.environ.copy()
	if proxy is not None and proxy != '' and proxy != 'None':
		parsed_proxy = urlparse(proxy)
		if parsed_proxy.scheme not in {'http', 'https'} or not parsed_proxy.hostname:
			raise ValueError('Proxy must be a valid HTTP or HTTPS URL')
		if any(character in proxy for character in ('\r', '\n', '\x00')):
			raise ValueError('Proxy contains invalid control characters')
		environment['HTTPS_PROXY'] = proxy

	for collection in collections:
		if not os.path.isdir(f'/usr/share/httpd/.ansible/collections/ansible_collections/{collection.replace(".", "/")}'):
			try:
				command = ['ansible-galaxy', 'collection', 'install', collection]
				if version.parse(ansible.__version__) < version.parse('2.13.9'):
					command.extend(['--server', 'https://old-galaxy.ansible.com/'])
				exit_code = subprocess.run(command, env=environment, check=False).returncode
			except Exception as e:
				roxywi_common.handle_exceptions(e,
												'Roxy-WI server',
												f'Cannot install as collection. {trouble_link}'
												)
			else:
				if exit_code != 0:
					raise Exception(f'error: Ansible collection installation was not successful: {exit_code}. {trouble_link}')


def run_ansible_thread(
		inv: dict, server_ips: list, ansible_role: str, service_name: str,
		on_success: Callable[[], None] = None
) -> int:
	server_ids = []
	claims = roxywi_common.get_jwt_token_claims()
	for server_ip in server_ips:
		server_id = server_sql.get_server_by_ip(server_ip).server_id
		server_ids.append(server_id)

	task_id = InstallationTasks.insert(
		service_name=service_name, server_ids=server_ids, user_id=claims['user_id'], group_id=claims['group']
	).execute()
	thread = threading.Thread(target=run_installations, args=(inv, server_ips, ansible_role, task_id, on_success))
	thread.start()
	return task_id


def run_installations(
		inv: dict, server_ips: list, service: str, task_id: int,
		on_success: Callable[[], None] = None
) -> None:
	InstallationTasks.update(status='running').where(InstallationTasks.id == task_id).execute()
	try:
		output = run_ansible(inv, server_ips, service)
		if output.get('failures') or output.get('dark'):
			raise RuntimeError(f'Cannot install {service}. Check Apache error log')
		if on_success is not None:
			on_success()
	except Exception as e:
		InstallationTasks.update(status='failed', finish_date=datetime.now(), error=str(e)).where(InstallationTasks.id == task_id).execute()
		roxywi_common.logging('', f'error: Cannot install {service}: {e}')
	else:
		InstallationTasks.update(
			status='completed', finish_date=datetime.now(), error=None
		).where(InstallationTasks.id == task_id).execute()
