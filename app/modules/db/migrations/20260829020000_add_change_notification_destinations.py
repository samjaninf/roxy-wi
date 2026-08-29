from peewee import TextField
from playhouse.migrate import migrate

from app.modules.db.db_model import connect


COLUMN_NAME = 'notification_destinations'


def _columns() -> set[str]:
    return {column.name for column in connect().get_columns('config_changes')}


def up():
    """Store concrete Change Center notification recipients."""
    if COLUMN_NAME not in _columns():
        migrator = connect(get_migrator=1)
        migrate(migrator.add_column(
            'config_changes', COLUMN_NAME, TextField(default='[]')
        ))


def down():
    """Remove concrete Change Center notification recipients."""
    if COLUMN_NAME in _columns():
        migrator = connect(get_migrator=1)
        migrate(migrator.drop_column('config_changes', COLUMN_NAME))
