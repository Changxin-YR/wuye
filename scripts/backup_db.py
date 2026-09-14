"""演示库备份 / 恢复工具（不依赖 mysqldump 客户端）。

用法::

    # 备份当前 .env 里 DATABASE_URL 指向的库（默认写到 artifacts/db-backup/）
    .venv\\Scripts\\python.exe scripts\\backup_db.py

    # 从备份恢复（危险：会先删同名表再建）
    .venv\\Scripts\\python.exe scripts\\backup_db.py --restore artifacts\\db-backup\\xxx.sql

输出：一份纯 SQL 文件（CREATE TABLE + INSERT），可直接用 mysql 客户端回放，
也可用本脚本的 --restore 回放。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

import config as app_config  # noqa: E402

BACKUP_DIR = ROOT / "artifacts" / "db-backup"


def _quote(value) -> str:
    """把 Python 值转成 SQL 字面量。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, datetime):
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
    text_value = str(value)
    text_value = (
        text_value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "")
    )
    return "'" + text_value + "'"


def backup(url: str | None = None, note: str = "", out: str | None = None) -> Path:
    """导出整个库为 SQL 文件，返回文件路径。"""
    url = url or app_config.get_config().DATABASE_URL
    engine = create_engine(url, connect_args={"connect_timeout": 10} if url.startswith("mysql") else {})
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 文件名安全化：Windows 不允许 : ? * " < > |
    safe = url.split("@")[-1]
    for ch in ':/\\?*"<>|':
        safe = safe.replace(ch, "_")
    safe = safe.strip("_") or "db"
    if out:
        candidate = Path(out)
        path = candidate if candidate.is_absolute() else (ROOT / candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = BACKUP_DIR / f"{stamp}-{safe}.sql"

    lines: list[str] = [
        f"-- 演示库备份 {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"-- 来源：{url.split('@')[-1]}",
        f"-- 说明：{note or 'reset 前的强制备份（capain 裁决：先备份后 drop）'}",
        "SET FOREIGN_KEY_CHECKS=0;",
        "",
    ]
    total_rows = 0
    with engine.connect() as conn:
        tables = conn.execute(text("SHOW TABLES")).scalars().all()
        for table in tables:
            ddl = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).first()[1]
            lines.append(f"-- ---------- {table} ----------")
            lines.append(f"DROP TABLE IF EXISTS `{table}`;")
            lines.append(ddl + ";")
            rows = conn.execute(text(f"SELECT * FROM `{table}`")).fetchall()
            if rows:
                columns = list(conn.execute(text(f"SELECT * FROM `{table}` LIMIT 0")).keys())
                col_sql = ", ".join(f"`{c}`" for c in columns)
                batch: list[str] = []
                for row in rows:
                    values = ", ".join(_quote(v) for v in row)
                    batch.append(f"({values})")
                    if len(batch) >= 100:
                        lines.append(f"INSERT INTO `{table}` ({col_sql}) VALUES\n  " + ",\n  ".join(batch) + ";")
                        batch = []
                if batch:
                    lines.append(f"INSERT INTO `{table}` ({col_sql}) VALUES\n  " + ",\n  ".join(batch) + ";")
                total_rows += len(rows)
            lines.append("")
        lines.append("SET FOREIGN_KEY_CHECKS=1;")

    path.write_text("\n".join(lines), encoding="utf-8")
    engine.dispose()
    print(f"[OK] 备份完成：{path.relative_to(ROOT)}（{len(tables)} 张表 / {total_rows} 行）")
    return path


def restore(path: str, url: str | None = None) -> None:
    """从 SQL 备份恢复（该文件自带 DROP TABLE，会先删再建）。"""
    url = url or app_config.get_config().DATABASE_URL
    engine = create_engine(url)
    sql = Path(path).read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";\n") if s.strip() and not s.strip().startswith("--")]
    with engine.begin() as conn:
        for statement in statements:
            if statement.upper().startswith(("SET ", "DROP ", "CREATE ", "INSERT ")):
                conn.execute(text(statement))
    engine.dispose()
    print(f"[OK] 已从 {path} 恢复（{len(statements)} 条语句）")


def main() -> int:
    parser = argparse.ArgumentParser(description="演示库备份 / 恢复")
    parser.add_argument("--restore", metavar="SQL_FILE", help="从指定 SQL 文件恢复")
    parser.add_argument("--note", default="", help="备份说明")
    parser.add_argument("--out", default=None, help="指定输出文件（相对项目根目录或绝对路径）")
    args = parser.parse_args()
    if args.restore:
        restore(args.restore)
        return 0
    backup(note=args.note, out=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
