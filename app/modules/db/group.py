from app.modules.db.db_model import Groups, Setting, UserGroups
from app.modules.db.common import out_error
from app.modules.roxywi.exception import RoxywiResourceNotFound


def select_groups():
	try:
		return Groups.select().order_by(Groups.group_id).execute()
	except Exception as e:
		out_error(e)


def add_group(name: str, description: str) -> int:
	try:
		last_id = Groups.insert(name=name, description=description).execute()
	except Exception as e:
		out_error(e)
	else:
		add_setting_for_new_group(last_id)
		return last_id


def add_setting_for_new_group(group_id):
	group_id = str(group_id)
	data_source = [
		{'param': 'time_zone', 'value': 'UTC', 'section': 'main', 'desc': 'Time Zone', 'group_id': group_id},
		{'param': 'proxy', 'value': '', 'section': 'main', 'desc': 'IP address and port of the proxy server . Use proto://ip:port', 'group_id': group_id},
		{'param': 'session_ttl', 'value': '5', 'section': 'main', 'desc': 'TTL for a user session (in days)', 'group_id': group_id},
		{'param': 'token_ttl', 'value': '5', 'section': 'main', 'desc': 'TTL for a user token (in days)', 'group_id': group_id},
		{'param': 'tmp_config_path', 'value': '/tmp/', 'section': 'main', 'desc': 'Path to the temporary directory.', 'group_id': group_id},
		{'param': 'cert_path', 'value': '/etc/ssl/certs/', 'section': 'main', 'desc': 'Path to SSL dir', 'group_id': group_id},
		{'param': 'haproxy_path_logs', 'value': '/var/log/haproxy/', 'section': 'haproxy', 'desc': 'The default local path for saving logs', 'group_id': group_id},
		{'param': 'syslog_server_enable', 'value': '0', 'section': 'logs', 'desc': 'Enable getting logs from a syslog server; (0 - no, 1 - yes)', 'group_id': group_id},
		{'param': 'syslog_server', 'value': '', 'section': 'logs', 'desc': 'IP address of the syslog_server', 'group_id': group_id},
		{'param': 'haproxy_stats_user', 'value': 'admin', 'section': 'haproxy', 'desc': 'Username for accessing HAProxy stats page', 'group_id': group_id},
		{'param': 'haproxy_stats_password', 'value': 'password', 'section': 'haproxy', 'desc': 'Password for accessing HAProxy stats page', 'group_id': group_id},
		{'param': 'haproxy_stats_port', 'value': '8085', 'section': 'haproxy', 'desc': 'Port for HAProxy stats page', 'group_id': group_id},
		{'param': 'haproxy_stats_page', 'value': 'stats', 'section': 'haproxy', 'desc': 'URI for HAProxy stats page', 'group_id': group_id},
		{'param': 'haproxy_dir', 'value': '/etc/haproxy', 'section': 'haproxy', 'desc': 'Path to the HAProxy directory', 'group_id': group_id},
		{'param': 'haproxy_config_path', 'value': '/etc/haproxy/haproxy.cfg', 'section': 'haproxy', 'desc': 'Path to the HAProxy configuration file', 'group_id': group_id},
		{'param': 'server_state_file', 'value': '/etc/haproxy/haproxy.state', 'section': 'haproxy', 'desc': 'Path to the HAProxy state file', 'group_id': group_id},
		{'param': 'haproxy_sock', 'value': '/var/run/haproxy.sock', 'section': 'haproxy', 'desc': 'Path to the HAProxy sock file', 'group_id': group_id},
		{'param': 'haproxy_sock_port', 'value': '1999', 'section': 'haproxy', 'desc': 'Socket port for HAProxy', 'group_id': group_id},
		{'param': 'haproxy_container_name', 'value': 'haproxy', 'section': 'haproxy', 'desc': 'Docker container name for HAProxy service', 'group_id': group_id},
		{'param': 'maxmind_key', 'value': '', 'section': 'main', 'desc': 'License key for downloading GeoIP DB. You can create it on maxmind.com', 'group_id': group_id},
		{'param': 'nginx_path_logs', 'value': '/var/log/nginx/', 'section': 'nginx', 'desc': 'NGINX error log', 'group_id': group_id},
		{'param': 'nginx_stats_user', 'value': 'admin', 'section': 'nginx', 'desc': 'Username for accessing NGINX stats page', 'group_id': group_id},
		{'param': 'nginx_stats_password', 'value': 'password', 'section': 'nginx', 'desc': 'Password for accessing NGINX stats page', 'group_id': group_id},
		{'param': 'nginx_stats_port', 'value': '8086', 'section': 'nginx', 'desc': 'Stats port for web page NGINX', 'group_id': group_id},
		{'param': 'nginx_stats_page', 'value': 'stats', 'section': 'nginx', 'desc': 'URI Stats for web page NGINX', 'group_id': group_id},
		{'param': 'nginx_dir', 'value': '/etc/nginx/', 'section': 'nginx', 'desc': 'Path to the NGINX directory with config files', 'group_id': group_id},
		{'param': 'nginx_config_path', 'value': '/etc/nginx/nginx.conf', 'section': 'nginx', 'desc': 'Path to the main NGINX configuration file', 'group_id': group_id},
		{'param': 'nginx_container_name', 'value': 'nginx', 'section': 'nginx', 'desc': 'Docker container name for NGINX service', 'group_id': group_id},
		{'param': 'ldap_enable', 'value': '0', 'section': 'ldap', 'desc': 'Enable LDAP', 'group_id': group_id},
		{'param': 'ldap_server', 'value': '', 'section': 'ldap', 'desc': 'IP address of the LDAP server', 'group_id': group_id},
		{'param': 'ldap_port', 'value': '389', 'section': 'ldap', 'desc': 'LDAP port (port 389 or 636 is used by default)', 'group_id': group_id},
		{'param': 'ldap_user', 'value': '', 'section': 'ldap', 'desc': 'LDAP username. Format: user@domain.com', 'group_id': group_id},
		{'param': 'ldap_password', 'value': '', 'section': 'ldap', 'desc': 'LDAP password', 'group_id': group_id},
		{'param': 'ldap_base', 'value': '', 'section': 'ldap', 'desc': 'Base domain. Example: dc=domain, dc=com', 'group_id': group_id},
		{'param': 'ldap_domain', 'value': '', 'section': 'ldap', 'desc': 'LDAP domain for logging in', 'group_id': group_id},
		{'param': 'ldap_class_search', 'value': 'user', 'section': 'ldap', 'desc': 'Class for searching the user', 'group_id': group_id},
		{'param': 'ldap_user_attribute', 'value': 'sAMAccountName', 'section': 'ldap', 'desc': 'Attribute to search users by', 'group_id': group_id},
		{'param': 'ldap_search_field', 'value': 'mail', 'section': 'ldap', 'desc': 'User\'s email address', 'group_id': group_id},
		{'param': 'ldap_type', 'value': '0', 'section': 'ldap', 'desc': 'Use LDAPS', 'group_id': group_id},
		{'param': 'apache_path_logs', 'value': '/var/log/httpd/', 'section': 'apache', 'desc': 'The path for Apache logs', 'group_id': group_id},
		{'param': 'apache_stats_user', 'value': 'admin', 'section': 'apache', 'desc': 'Username for accessing Apache stats page', 'group_id': group_id},
		{'param': 'apache_stats_password', 'value': 'password', 'section': 'apache', 'desc': 'Password for Apache stats webpage', 'group_id': group_id},
		{'param': 'apache_stats_port', 'value': '8087', 'section': 'apache', 'desc': 'Stats port for webpage Apache', 'group_id': group_id},
		{'param': 'apache_stats_page', 'value': 'stats', 'section': 'apache', 'desc': 'URI Stats for webpage Apache', 'group_id': group_id},
		{'param': 'apache_dir', 'value': '/etc/httpd/', 'section': 'apache', 'desc': 'Path to the Apache directory with config files', 'group_id': group_id},
		{'param': 'apache_config_path', 'value': '/etc/httpd/conf/httpd.conf', 'section': 'apache', 'desc': 'Path to the main Apache configuration file', 'group_id': group_id},
		{'param': 'apache_container_name', 'value': 'apache', 'section': 'apache', 'desc': 'Docker container name for Apache service', 'group_id': group_id},
		{'param': 'keepalived_config_path', 'value': '/etc/keepalived/keepalived.conf', 'section': 'keepalived', 'desc': 'Path to the main Keepalived configuration file', 'group_id': group_id},
		{'param': 'keepalived_path_logs', 'value': '/var/log/keepalived/', 'section': 'keepalived', 'desc': 'The path for Keepalived logs', 'group_id': group_id},
	]

	try:
		Setting.insert_many(data_source).execute()
	except Exception as e:
		out_error(e)


def delete_group(group_id):
	try:
		Groups.delete().where(Groups.group_id == group_id).execute()
		UserGroups.delete().where(UserGroups.user_group_id == group_id).execute()
	except Exception as e:
		out_error(e)
		return False
	else:
		delete_group_settings(group_id)
		return True


def delete_group_settings(group_id):
	try:
		Setting.delete().where(Setting.group_id == group_id).execute()
	except Exception as e:
		out_error(e)


def update_group(name, descript, group_id):
	try:
		Groups.update(name=name, description=descript).where(Groups.group_id == group_id).execute()
	except Exception as e:
		out_error(e)


def get_group(group_id: int) -> Groups:
	try:
		return Groups.get(Groups.group_id == group_id)
	except Groups.DoesNotExist:
		raise RoxywiResourceNotFound
	except Exception as e:
		out_error(e)
