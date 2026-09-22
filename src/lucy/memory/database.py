import sqlite3

class MemoryDatabase:
    def __init__(self):
        # Create the central memory database
        self.conn = sqlite3.connect("C:/Users/dimay/Lucy/Lucy_Core/runtime/memory.sqlite")
        self.cursor = self.conn.cursor()
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY,
                category TEXT,
                key TEXT,
                value TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()
