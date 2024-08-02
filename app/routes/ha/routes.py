from flask import render_template, g, request
from flask_jwt_extended import jwt_required

from app.routes.ha import bp
from app.middleware import get_user_params, check_services
import app.modules.db.ha_cluster as ha_sql
import app.modules.db.server as server_sql
import app.modules.db.service as service_sql
import app.modules.server.server as server_mod
import app.modules.roxywi.common as roxywi_common
import app.modules.service.keepalived as keepalived
from app.views.ha.views import HAView, HAVIPView, HAVIPsView


# def register_api(view, endpoint, url, pk='listener_id', pk_type='int'):
#     view_func = view.as_view(endpoint)
#     bp.add_url_rule(url, view_func=view_func, methods=['GET'], defaults={pk: None})
#     bp.add_url_rule(url, view_func=view_func, methods=['POST'])
#     bp.add_url_rule(f'{url}/<{pk_type}:{pk}>', view_func=view_func, methods=['GET', 'PUT', 'DELETE'])

# register_api(HAView, 'ha_cluster', '/<service>', 'cluster_id')
# bp.add_url_rule('/<service>/<int:cluster_id>/vip/<int:router_id>', view_func=HAVIPView.as_view('ha_vip_g'), methods=['GET'])
bp.add_url_rule('/<service>', view_func=HAView.as_view('ha_cluster'), methods=['GET'], defaults={'cluster_id': None})
# bp.add_url_rule('/<service>/<int:router_id>/vip', view_func=HAVIPView.as_view('ha_vip_d'), methods=['DELETE'])
# bp.add_url_rule('/<service>/<int:cluster_id>/vips', view_func=HAVIPsView.as_view('ha_vips'), methods=['GET'])


@bp.before_request
@jwt_required()
def before_request():
    """ Protect all the admin endpoints. """
    pass


@bp.route('/<service>/get/<int:cluster_id>')
@check_services
@get_user_params()
def get_ha_cluster(service, cluster_id):
    router_id = ha_sql.get_router_id(cluster_id, default_router=1)
    kwargs = {
        'servers': roxywi_common.get_dick_permit(virt=1),
        'clusters': ha_sql.select_cluster(cluster_id),
        'slaves': ha_sql.select_cluster_slaves(cluster_id, router_id),
        'virts': ha_sql.select_clusters_virts(),
        'vips': ha_sql.select_cluster_vips(cluster_id),
        'cluster_services': ha_sql.select_cluster_services(cluster_id),
        'services': service_sql.select_services(),
        'group_id': g.user_params['group_id'],
        'router_id': router_id,
        'lang': g.user_params['lang']
    }

    return render_template('ajax/ha/clusters.html', **kwargs)


@bp.route('/<service>/<int:cluster_id>')
@check_services
@get_user_params()
def show_ha_cluster(service, cluster_id):
    services = []
    service = 'keepalived'
    service_desc = service_sql.select_service(service)
    router_id = ha_sql.get_router_id(cluster_id, default_router=1)
    servers = ha_sql.select_cluster_master_slaves(cluster_id, g.user_params['group_id'], router_id)
    waf_server = ''
    cmd = "ps ax |grep -e 'keep_alive.py' |grep -v grep |wc -l"
    keep_alive, stderr = server_mod.subprocess_execute(cmd)
    servers_with_status1 = []
    restart_settings = service_sql.select_restart_services_settings(service_desc.slug)
    for s in servers:
        servers_with_status = list()
        servers_with_status.append(s[0])
        servers_with_status.append(s[1])
        servers_with_status.append(s[2])
        servers_with_status.append(s[11])
        status1, status2 = keepalived.get_status(s[2])
        servers_with_status.append(status1)
        servers_with_status.append(status2)
        servers_with_status.append(s[22])
        servers_with_status.append(server_sql.is_master(s[2]))
        servers_with_status.append(server_sql.select_servers(server=s[2]))

        is_keepalived = service_sql.select_keepalived(s[2])

        if is_keepalived:
            try:
                cmd = ('sudo kill -USR1 `cat /var/run/keepalived.pid` && sudo grep State /tmp/keepalived.data -m 1 |'
                       'awk -F"=" \'{print $2}\'|tr -d \'[:space:]\' && sudo rm -f /tmp/keepalived.data')
                out = server_mod.ssh_command(s[2], cmd)
                out1 = ('1', out)
                servers_with_status.append(out1)
            except Exception as e:
                servers_with_status.append(str(e))
        else:
            servers_with_status.append('')

        servers_with_status1.append(servers_with_status)

    user_subscription = roxywi_common.return_user_subscription()
    kwargs = {
        'servers': servers_with_status1,
        'waf_server': waf_server,
        'service': service,
        'services': services,
        'service_desc': service_desc,
        'keep_alive': ''.join(keep_alive),
        'restart_settings': restart_settings,
        'user_subscription': user_subscription,
        'clusters': ha_sql.select_ha_cluster_name_and_slaves(),
        'master_slave': server_sql.is_master(0, master_slave=1),
        'lang': g.user_params['lang']
    }

    return render_template('service.html', **kwargs)


@bp.route('/<service>/slaves/<int:cluster_id>', methods=['GET', 'POST'])
@check_services
@get_user_params()
def get_slaves(service, cluster_id):
    lang = g.user_params['lang']
    if request.method == 'GET':
        router_id = ha_sql.get_router_id(cluster_id, default_router=1)
    else:
        router_id = int(request.form.get('router_id'))
    slaves = ha_sql.select_cluster_slaves(cluster_id, router_id)

    return render_template('ajax/ha/add_vip_slaves.html', lang=lang, slaves=slaves)


@bp.route('/<service>/slaves/servers/<int:cluster_id>')
@check_services
@get_user_params()
def get_server_slaves(service, cluster_id):
    group_id = g.user_params['group_id']
    lang = g.user_params['lang']
    try:
        router_id = ha_sql.get_router_id(cluster_id, default_router=1)
        slaves = ha_sql.select_cluster_slaves(cluster_id, router_id)
    except Exception:
        slaves = ''
    free_servers = ha_sql.select_ha_cluster_not_masters_not_slaves(group_id)

    return render_template('ajax/ha/slave_servers.html', free_servers=free_servers, slaves=slaves, lang=lang)


@bp.route('/<service>/masters')
@check_services
@get_user_params()
def get_masters(service):
    group_id = g.user_params['group_id']
    free_servers = ha_sql.select_ha_cluster_not_masters_not_slaves(group_id)

    return render_template('ajax/ha/masters.html', free_servers=free_servers)
