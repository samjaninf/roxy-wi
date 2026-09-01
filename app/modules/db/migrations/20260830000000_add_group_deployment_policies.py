from app.modules.db.db_model import Groups, Setting


SERVICES = ('haproxy', 'nginx', 'apache', 'keepalived')
PARAMS = tuple(f'{service}_deployment_mode' for service in SERVICES)


def up():
    """Give every existing group a backward-compatible deployment policy."""
    rows = [
        {
            'param': f'{service}_deployment_mode',
            'value': 'both',
            'section': 'change_center',
            'desc': f'Configuration deployment mode for {service}',
            'group_id': group.group_id,
        }
        for group in Groups.select(Groups.group_id)
        for service in SERVICES
    ]
    if rows:
        Setting.insert_many(rows).on_conflict_ignore().execute()


def down():
    """Remove group deployment policy settings."""
    Setting.delete().where(Setting.param.in_(PARAMS)).execute()
