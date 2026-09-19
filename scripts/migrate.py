from app.db import init_db


if __name__ == "__main__":
    print(f"数据库迁移完成：{init_db()}")
