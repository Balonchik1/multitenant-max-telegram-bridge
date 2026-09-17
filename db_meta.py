import aiosqlite

# путь задаётся из bridge при старте или оставляем default
DB_PATH = "bridge.db"

def set_db_path(path: str):
    global DB_PATH
    DB_PATH = path

# ----- ping -----
# db_path необязателен (по умолчанию — твоя основная база) — тенанты
# передают свою собственную, та же логика, что и для aliases ниже.
async def list_pings(db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        async with db.execute(
            "SELECT keyword, tg_username, tg_id FROM ping_map ORDER BY keyword"
        ) as cur:
            return await cur.fetchall()

async def set_ping(keyword: str, tg_username: str, tg_id: int | None = None, db_path: str | None = None):
    keyword = keyword.strip().lower()
    uname = (tg_username or "").strip().lstrip("@")
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ping_map (keyword, tg_username, tg_id)
            VALUES (?, ?, ?)
            ON CONFLICT(keyword) DO UPDATE SET
                tg_username = excluded.tg_username,
                tg_id = excluded.tg_id
            """,
            (keyword, uname, tg_id),
        )
        await db.commit()

async def delete_ping(keyword: str, db_path: str | None = None):
    key = keyword.strip().lower()
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute("DELETE FROM ping_map WHERE keyword = ?", (key,))
        await db.commit()

# ----- alias -----
# db_path необязателен (по умолчанию — твоя основная база) — тенанты
# передают свою собственную, чтобы их алиасы не смешивались ни с твоими,
# ни с алиасами других тенантов.
async def list_aliases(db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        async with db.execute(
            "SELECT max_user_id, last_name FROM aliases ORDER BY max_user_id"
        ) as cur:
            return await cur.fetchall()

async def get_alias(max_user_id: int, db_path: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        async with db.execute(
            "SELECT last_name FROM aliases WHERE max_user_id = ?",
            (max_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_alias(max_user_id: int, last_name: str, db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO aliases (max_user_id, last_name) VALUES (?, ?)
            ON CONFLICT(max_user_id) DO UPDATE SET last_name = excluded.last_name
            """,
            (max_user_id, last_name),
        )
        await db.commit()

async def delete_alias(max_user_id: int, db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute("DELETE FROM aliases WHERE max_user_id = ?", (max_user_id,))
        await db.commit()
        
async def list_mention_links(db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        async with db.execute(
            "SELECT max_user_id, tg_username, tg_id FROM mention_map ORDER BY max_user_id"
        ) as cur:
            return await cur.fetchall()

async def get_mention_link(max_user_id: int, db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        async with db.execute(
            "SELECT tg_username, tg_id FROM mention_map WHERE max_user_id = ?",
            (max_user_id,),
        ) as cur:
            return await cur.fetchone()

async def set_mention_link(max_user_id: int, tg_username: str, tg_id: int | None = None, db_path: str | None = None):
    uname = (tg_username or "").strip().lstrip("@")
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO mention_map (max_user_id, tg_username, tg_id)
            VALUES (?, ?, ?)
            ON CONFLICT(max_user_id) DO UPDATE SET
                tg_username = excluded.tg_username,
                tg_id = excluded.tg_id
            """,
            (max_user_id, uname, tg_id),
        )
        await db.commit()

async def delete_mention_link(max_user_id: int, db_path: str | None = None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute(
            "DELETE FROM mention_map WHERE max_user_id = ?",
            (max_user_id,),
        )
        await db.commit()