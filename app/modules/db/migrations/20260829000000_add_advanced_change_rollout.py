from peewee import CharField, IntegerField, TextField
from playhouse.migrate import migrate

from app.modules.db.db_model import connect


CHANGE_COLUMNS = (
    ('batch_size', IntegerField(default=0)),
    ('max_parallel', IntegerField(default=8)),
    ('manual_promotion', IntegerField(default=0)),
    ('health_check_mode', CharField(default='full')),
    ('health_check_retries', IntegerField(default=1)),
    ('health_check_interval', IntegerField(default=0)),
    ('pause_requested', IntegerField(default=0)),
)

TARGET_COLUMNS = (
    ('batch', IntegerField(default=0)),
    ('is_canary', IntegerField(default=0)),
    ('excluded', IntegerField(default=0)),
    ('excluded_reason', TextField(null=True)),
    ('health_output', TextField(null=True)),
)


def _columns(table_name: str) -> set[str]:
    return {column.name for column in connect().get_columns(table_name)}


def up():
    """Add advanced rollout controls without changing existing rollout defaults."""
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


def down():
    """Remove advanced rollout controls."""
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
