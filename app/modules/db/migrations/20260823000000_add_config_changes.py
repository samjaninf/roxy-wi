from app.modules.db.db_model import ConfigChange, connect


def up():
    """Create the persistent Change Center workflow table."""
    connection = connect()
    connection.create_tables([ConfigChange], safe=True)


def down():
    """Remove the Change Center workflow table."""
    connection = connect()
    connection.drop_tables([ConfigChange], safe=True)
