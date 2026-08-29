from peewee import CharField, DateTimeField, TextField
from playhouse.migrate import migrate

from app.modules.db.db_model import (
    ConfigChangeDelivery,
    ConfigChangeEvent,
    ConfigChangeWebhook,
    connect,
)


CHANGE_COLUMNS = (
    ('scheduled_at', DateTimeField(null=True)),
    ('maintenance_window_end', DateTimeField(null=True)),
    ('schedule_base_status', CharField(null=True)),
    ('notification_channels', TextField(default='[]')),
    ('drift_status', CharField(default='unknown')),
    ('drift_checked_at', DateTimeField(null=True)),
    ('drift_diff', TextField(null=True)),
    ('started_at', DateTimeField(null=True)),
    ('finished_at', DateTimeField(null=True)),
)

TARGET_COLUMNS = (
    ('drift_status', CharField(default='unknown')),
    ('drift_checked_at', DateTimeField(null=True)),
    ('drift_diff', TextField(null=True)),
)


def _columns(table_name: str) -> set[str]:
    return {column.name for column in connect().get_columns(table_name)}


def up():
    """Add Change Center scheduling, drift, timeline and delivery persistence."""
    connection = connect()
    migrator = connect(get_migrator=1)
    operations = []
    change_columns = _columns('config_changes')
    target_columns = _columns('config_change_targets')
    for name, field in CHANGE_COLUMNS:
        if name not in change_columns:
            operations.append(migrator.add_column('config_changes', name, field))
    for name, field in TARGET_COLUMNS:
        if name not in target_columns:
            operations.append(migrator.add_column('config_change_targets', name, field))
    if operations:
        migrate(*operations)
    connection.create_tables(
        [ConfigChangeEvent, ConfigChangeWebhook, ConfigChangeDelivery], safe=True
    )


def down():
    """Remove Change Center automation and visibility storage."""
    connection = connect()
    connection.drop_tables(
        [ConfigChangeDelivery, ConfigChangeWebhook, ConfigChangeEvent], safe=True
    )
    migrator = connect(get_migrator=1)
    operations = []
    target_columns = _columns('config_change_targets')
    change_columns = _columns('config_changes')
    for name, _field in reversed(TARGET_COLUMNS):
        if name in target_columns:
            operations.append(migrator.drop_column('config_change_targets', name))
    for name, _field in reversed(CHANGE_COLUMNS):
        if name in change_columns:
            operations.append(migrator.drop_column('config_changes', name))
    if operations:
        migrate(*operations)
