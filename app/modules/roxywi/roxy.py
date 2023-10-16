import os
import re

import distro
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

import modules.db.sql as sql
import modules.roxywi.common as roxywi_common
import app.modules.server.server as server_mod


def is_docker() -> bool:
	path = "/proc/self/cgroup"
	if not os.path.isfile(path):
		return False
	with open(path) as f:
		for line in f:
			if re.match("\d+:[\w=]+:/docker(-[ce]e)?/\w+", line):
				return True
		return False


def check_ver():
	return sql.get_ver()


def versions():
	try:
		current_ver = check_ver()
		current_ver_without_dots = current_ver.split('.')
		current_ver_without_dots = ''.join(current_ver_without_dots)
		current_ver_without_dots = current_ver_without_dots.replace('\n', '')
		if len(current_ver_without_dots) == 4:
			current_ver_without_dots += '0'
		current_ver_without_dots = int(current_ver_without_dots)
	except Exception:
		current_ver = "Cannot get current version"
		current_ver_without_dots = 0

	try:
		new_ver = check_new_version('roxy-wi')
		new_ver_without_dots = new_ver.split('.')
		new_ver_without_dots = ''.join(new_ver_without_dots)
		new_ver_without_dots = new_ver_without_dots.replace('\n', '')
		new_ver_without_dots = int(new_ver_without_dots)
	except Exception as e:
		new_ver = "Cannot get a new version"
		new_ver_without_dots = 0
		roxywi_common.logging('Roxy-WI server', f' {e}', roxywi=1)

	return current_ver, new_ver, current_ver_without_dots, new_ver_without_dots


def check_new_version(service):
	current_ver = check_ver()
	proxy = sql.get_setting('proxy')
	res = ''
	proxy_dict = {}

	try:
		if proxy is not None and proxy != '' and proxy != 'None':
			proxy_dict = {"https": proxy, "http": proxy}
		response = requests.get(f'https://roxy-wi.org/version/get/{service}', timeout=1, proxies=proxy_dict)
		if service == 'roxy-wi':
			requests.get(f'https://roxy-wi.org/version/send/{current_ver}', timeout=1, proxies=proxy_dict)

		res = response.content.decode(encoding='UTF-8')
	except requests.exceptions.RequestException as e:
		roxywi_common.logging('Roxy-WI server', f' {e}', roxywi=1)

	return res


def update_user_status() -> None:
	proxy = sql.get_setting('proxy')
	proxy_dict = {}
	if proxy is not None and proxy != '' and proxy != 'None':
		proxy_dict = {"https": proxy, "http": proxy}
	user_name = sql.select_user_name()
	retry_strategy = Retry(
		total=3,
		status_forcelist=[429, 500, 502, 503, 504],
		method_whitelist=["HEAD", "GET", "OPTIONS"]
	)
	adapter = HTTPAdapter(max_retries=retry_strategy)
	roxy_wi_get_plan = requests.Session()
	roxy_wi_get_plan.mount("https://", adapter)
	roxy_wi_get_plan = requests.get(f'https://roxy-wi.org/user-name/{user_name}', timeout=1, proxies=proxy_dict)
	try:
		status = roxy_wi_get_plan.content.decode(encoding='UTF-8')
		status = status.split(' ')
		sql.update_user_status(status[0], status[1].strip(), status[2].strip())
	except Exception as e:
		roxywi_common.logging('Roxy-WI server', f'error: Cannot get user status {e}', roxywi=1)


def action_service(action: str, service: str) -> str:
	is_in_docker = is_docker()
	cmd = f"sudo systemctl disable {service} --now"
	if action in ("start", "restart"):
		cmd = f"sudo systemctl {action} {service} --now"
		if not sql.select_user_status():
			return 'warning: The service is disabled because you are not subscribed. Read <a href="https://roxy-wi.org/pricing" ' \
				   'title="Roxy-WI pricing" target="_blank">here</a> about subscriptions'
	if is_in_docker:
		cmd = f"sudo supervisorctl {action} {service}"
	os.system(cmd)
	roxywi_common.logging('Roxy-WI server', f' The service {service} has been {action}ed', roxywi=1, login=1)
	return 'ok'


def update_plan():
	try:
		if distro.id() == 'ubuntu':
			path_to_repo = '/etc/apt/auth.conf.d/roxy-wi.conf'
			cmd = "grep login /etc/apt/auth.conf.d/roxy-wi.conf |awk '{print $2}'"
		else:
			path_to_repo = '/etc/yum.repos.d/roxy-wi.repo'
			cmd = "grep base /etc/yum.repos.d/roxy-wi.repo |awk -F\":\" '{print $2}'|awk -F\"/\" '{print $3}'"
		if os.path.exists(path_to_repo):
			get_user_name, stderr = server_mod.subprocess_execute(cmd)
			user_name = get_user_name[0]
		else:
			user_name = 'git'

		if sql.select_user_name():
			sql.update_user_name(user_name)
		else:
			sql.insert_user_name(user_name)
	except Exception as e:
		roxywi_common.logging('Cannot update subscription: ', str(e), roxywi=1)

	update_user_status()
