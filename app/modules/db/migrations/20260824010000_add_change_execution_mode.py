from peewee import CharField
from playhouse.migrate import migrate

from app.modules.db.db_model import connect


def _has_column() -> bool:
    database = connect()
    return 'execution_mode' in {
        column.name for column in database.get_columns('config_changes')
    }


def up():
    """Add the rollout execution mode while preserving rolling as the safe default."""
    if _has_column():
        return
    migrator = connect(get_migrator=1)
    migrate(
        migrator.add_column(
            'config_changes', 'execution_mode', CharField(default='rolling')
        )
    )


def down():
    """Remove the rollout execution mode."""
    if not _has_column():
        return
    migrator = connect(get_migrator=1)
    migrate(migrator.drop_column('config_changes', 'execution_mode'))
