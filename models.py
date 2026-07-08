# models.py - FIXED VERSION
import pyodbc
from config import DB_CONFIG

def get_db():
    """Connect to SQL Server database."""
    connection_string = (
        f"Driver={DB_CONFIG['Driver']};"
        f"Server={DB_CONFIG['Server']};"
        f"Database={DB_CONFIG['Database']};"
        f"Trusted_Connection={DB_CONFIG['Trusted_Connection']};"
    )
    return pyodbc.connect(connection_string)