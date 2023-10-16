import os
import sys
import pytz

from flask import render_template, request, redirect, url_for, session, g, abort
from flask_login import login_required

sys.path.append(os.path.join(sys.path[0], '/var/www/haproxy-wi/app'))

from app import app, cache
from app.routes.main import bp
import modules.db.sql as sql
from modules.db.db_model import conn
from middleware import check_services, get_user_params
import modules.common.common as common
import modules.roxywi.roxy as roxy
import modules.roxywi.auth as roxywi_auth
import modules.roxywi.nettools as nettools_mod
import modules.roxywi.common as roxywi_common
import modules.service.common as service_common
import modules.service.haproxy as service_haproxy


@app.errorhandler(403)
@get_user_params()
def page_is_forbidden(e):
    user_params = g.user_params
    return render_template(
        'error.html', user=user_params['user'], role=user_params['role'], user_services=user_params['user_services'], 
        title=e, e=e
    ), 403


@app.errorhandler(404)
@get_user_params()
def page_not_found(e):
    user_params = g.user_params
    return render_template(
        'error.html', user=user_params['user'], role=user_params['role'], user_services=user_params['user_services'],
        title=e, e=e
    ), 404


@app.errorhandler(405)
@get_user_params()
def method_not_allowed(e):
    user_params = g.user_params
    return render_template(
        'error.html', user=user_params['user'], role=user_params['role'], user_services=user_params['user_services'],
        title=e, e=e
    ), 405


@app.errorhandler(500)
@get_user_params()
def internal_error(e):
    user_params = g.user_params
    return render_template(
        'error.html', user=user_params['user'], role=user_params['role'], user_services=user_params['user_services'], 
        title=e, e=e
    ), 500


@app.before_request
def make_session_permanent():
    session.permanent = True


@app.teardown_request
def _db_close(exc):
    if not conn.is_closed():
        conn.close()


@bp.route('/stats/<service>/', defaults={'serv': None})
@bp.route('/stats/<service>/<serv>')
@login_required
@check_services
@get_user_params()
def stats(service, serv):
    user_params = g.user_params
    service_desc = sql.select_service(service)

    try:
        if serv is None:
            first_serv = user_params['servers']
            for i in first_serv:
                serv = i[2]
                break
    except Exception:
        pass

    servers = roxywi_common.get_dick_permit(service=service_desc.slug)
    return render_template(
        'statsview.html', autorefresh=1, role=user_params['role'], user=user_params['user'], selects=servers, serv=serv,
        service=service, user_services=user_params['user_services'], token=user_params['token'],
        select_id="serv", lang=user_params['lang'], service_desc=service_desc
    )


@bp.route('/stats/view/<service>/<server_ip>')
@login_required
@check_services
def show_stats(service, server_ip):
    server_ip = common.is_ip_or_dns(server_ip)

    if service in ('nginx', 'apache'):
        return service_common.get_stat_page(server_ip, service)
    else:
        return service_haproxy.stat_page_action(server_ip)


@bp.route('/nettools')
@login_required
@get_user_params(1)
def nettools():
    import time
    user_params = g.user_params
    return render_template(
        'nettools.html', autorefresh=0, role=user_params['role'], user=user_params['user'], servers=user_params['servers'],
        user_services=user_params['user_services'], token=user_params['token'], lang=user_params['lang']
    )


@bp.post('/nettols/<check>')
@login_required
def nettols_check(check):
    server_from = common.checkAjaxInput(request.form.get('server_from'))
    server_to = common.is_ip_or_dns(request.form.get('server_to'))
    action = common.checkAjaxInput(request.form.get('nettools_action'))
    port_to = common.checkAjaxInput(request.form.get('nettools_telnet_port_to'))
    dns_name = common.checkAjaxInput(request.form.get('nettools_nslookup_name'))
    dns_name = common.is_ip_or_dns(dns_name)
    record_type = common.checkAjaxInput(request.form.get('nettools_nslookup_record_type'))

    if check == 'icmp':
        return nettools_mod.ping_from_server(server_from, server_to, action)
    elif check == 'tcp':
        return nettools_mod.telnet_from_server(server_from, server_to, port_to)
    elif check == 'dns':
        return nettools_mod.nslookup_from_server(server_from, dns_name, record_type)
    else:
        return 'error: Wrong check'


@bp.route('/history/<service>/<server_ip>')
@login_required
@get_user_params()
def service_history(service, server_ip):
    users = sql.select_users()
    server_ip = common.checkAjaxInput(server_ip)
    user_subscription = roxywi_common.return_user_subscription()
    user_params = g.user_params

    if service in ('haproxy', 'nginx', 'keepalived', 'apache'):
        service_desc = sql.select_service(service)
        if not roxywi_auth.is_access_permit_to_service(service_desc.slug):
            abort(403, f'You do not have needed permissions to access to {service_desc.slug.title()} service')
        server_id = sql.select_server_id_by_ip(server_ip)
        history = sql.select_action_history_by_server_id_and_service(server_id, service_desc.service)
    elif service == 'server':
        if roxywi_common.check_is_server_in_group(server_ip):
            server_id = sql.select_server_id_by_ip(server_ip)
            history = sql.select_action_history_by_server_id(server_id)
    elif service == 'user':
        history = sql.select_action_history_by_user_id(server_ip)

    return render_template(
        'history.html', role=user_params['role'], user=user_params['user'], users=users, serv=server_ip, service=service,
        history=history, user_services=user_params['user_services'], token=user_params['token'],
        user_status=user_subscription['user_status'], user_plan=user_subscription['user_plan'], lang=user_params['lang']
    )


@bp.route('/servers')
@login_required
@get_user_params()
def servers():
    roxywi_auth.page_for_admin(level=2)

    user_params = g.user_params
    ldap_enable = sql.get_setting('ldap_enable')
    user_group = roxywi_common.get_user_group(id=1)
    settings = sql.get_setting('', all=1)
    services = sql.select_services()
    gits = sql.select_gits()
    servers = roxywi_common.get_dick_permit(virt=1, disable=0, only_group=1)
    masters = sql.select_servers(get_master_servers=1, uuid=user_params['user_uuid'])
    is_needed_tool = common.is_tool('ansible')
    user_roles = sql.select_user_roles_by_group(user_group)
    backups = sql.select_backups()
    s3_backups = sql.select_s3_backups()
    user_subscription = roxywi_common.return_user_subscription()

    if user_params['lang'] == 'ru':
        title = 'Сервера: '
    else:
        title = "Servers: "

    return render_template(
        'servers.html',
        h2=1, title=title, role=user_params['role'], user=user_params['user'], users=sql.select_users(group=user_group),
        groups=sql.select_groups(), servers=servers, roles=sql.select_roles(), sshs=sql.select_ssh(group=user_group),
        masters=masters, group=user_group, services=services, timezones=pytz.all_timezones, guide_me=1,
        token=user_params['token'], settings=settings, backups=backups, s3_backups=s3_backups, page="servers.py",
         user_services=user_params['user_services'], ldap_enable=ldap_enable,
        user_status=user_subscription['user_status'], user_plan=user_subscription['user_plan'], gits=gits,
        is_needed_tool=is_needed_tool, lang=user_params['lang'], user_roles=user_roles
    )


@bp.route('/internal/show_version')
@cache.cached()
def show_roxywi_version():
    return render_template('ajax/check_version.html', versions=roxy.versions())
