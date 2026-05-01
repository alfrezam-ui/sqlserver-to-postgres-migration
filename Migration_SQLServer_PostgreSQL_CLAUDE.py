"""
Data Migration Pipeline: SQL Server → PostgreSQL
================================================
Versión corregida con:
  - Conexión única reutilizable por base de datos
  - Migración real con execute_values (batch insert)
  - Lectura por chunks para tablas grandes
  - Orden correcto de migración respetando FK
  - Transacciones con rollback automático
  - Validaciones vectorizadas (sin loops fila por fila)
  - Sin exposición de credenciales en logs
"""

import os
import re
import logging
import pandas as pd
import pyodbc
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 1. LOGGING (reemplaza todos los print sensibles)
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 2. CONFIGURACIÓN
# ──────────────────────────────────────────────
load_dotenv()

CHUNK_SIZE = 5_000          # filas por lote al leer y al insertar
MAX_ERRORS = 3              # aborta la migración tras N errores seguidos

# Orden correcto respetando dependencias de FK:
#   Person.Address          → sin dependencias
#   Production.ProductCategory → sin dependencias
#   Sales.Customer          → depende de Person.Address
#   Purchasing.ProductVendor → depende de Production.ProductCategory
TABLES_ORDERED = [
    "Person.Address",
    "Production.ProductCategory",
    "Sales.Customer",
    "Purchasing.ProductVendor",
]

# Mapeo SQL Server schema.tabla → tabla destino en PostgreSQL (sin schema)
PG_TABLE_MAP = {
    "Person.Address":                "person_address",
    "Production.ProductCategory":    "production_productcategory",
    "Sales.Customer":                "sales_customer",
    "Purchasing.ProductVendor":      "purchasing_productvendor",
}

# ──────────────────────────────────────────────
# 3. CONEXIONES  (una sola vez cada una)
# ──────────────────────────────────────────────

def connect_sql_server() -> pyodbc.Connection:
    """Abre y devuelve una conexión a SQL Server."""
    conn_str = (
        f"SERVER={os.getenv('SQLSERVER_HOST')};"
        f"DATABASE={os.getenv('SQLSERVER_DB')};"
        f"UID={os.getenv('SQLSERVER_USER')};"
        f"PWD={os.getenv('SQLSERVER_PASSWORD')};"
        f"DRIVER={{SQL Server}};"
        "TrustServerCertificate=yes;"
    )
    try:
        conn = pyodbc.connect(conn_str)
        log.info("Conexión a SQL Server establecida.")
        return conn
    except pyodbc.Error as e:
        log.error("Error al conectar a SQL Server: %s", e)
        raise


def connect_postgres() -> psycopg2.extensions.connection:
    """Abre y devuelve una conexión a PostgreSQL."""
    try:
        conn = psycopg2.connect(
            host=os.getenv("PostgreSQL_HOST"),
            port=os.getenv("PostgreSQL_PORT", 5432),
            database=os.getenv("PostgreSQL_DB"),
            user=os.getenv("PostgreSQL_USER"),
            password=os.getenv("PostgreSQL_PASSWORD"),
        )
        conn.autocommit = False          # manejamos transacciones manualmente
        version = conn.cursor()
        version.execute("SELECT version();")
        log.info("Conexión a PostgreSQL establecida. Versión: %s", version.fetchone()[0][:60])
        return conn
    except psycopg2.OperationalError as e:
        log.error("Error al conectar a PostgreSQL: %s", e)
        raise


# ──────────────────────────────────────────────
# 4. PRE-MIGRATION CHECKS
# ──────────────────────────────────────────────

def check_row_counts(sql_conn: pyodbc.Connection) -> dict:
    """Cuenta filas en cada tabla origen y devuelve el baseline."""
    cursor = sql_conn.cursor()
    counts = {}
    log.info("=" * 55)
    log.info("CHECK 1: Conteo de filas (baseline)")
    log.info("=" * 55)
    for table in TABLES_ORDERED:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        n = cursor.fetchone()[0]
        counts[table] = n
        log.info("  %-35s %10d filas", table, n)
    log.info("  %-35s %10d filas (total)", "TOTAL", sum(counts.values()))
    return counts


def check_data_quality(sql_conn: pyodbc.Connection) -> list:
    """Ejecuta controles de calidad y devuelve lista de issues encontrados."""
    cursor = sql_conn.cursor()
    issues = []

    log.info("=" * 55)
    log.info("CHECK 2: Valores NULL en Purchasing.ProductVendor")
    log.info("=" * 55)
    cursor.execute(
        "SELECT COUNT(*) FROM Purchasing.ProductVendor WHERE OnOrderQty IS NULL"
    )
    n = cursor.fetchone()[0]
    if n:
        msg = f"  {n:,} filas con OnOrderQty NULL"
        issues.append(msg)
        log.warning(msg)
    else:
        log.info("  Sin NULLs en OnOrderQty.")

    log.info("CHECK 3: Productos con cantidad mínima de pedido = 1")
    cursor.execute(
        "SELECT COUNT(*) FROM Purchasing.ProductVendor WHERE MinOrderQty <= 1"
    )
    n = cursor.fetchone()[0]
    if n:
        msg = f"  {n:,} productos con MinOrderQty <= 1"
        issues.append(msg)
        log.warning(msg)
    else:
        log.info("  Sin productos con MinOrderQty <= 1.")

    log.info("CHECK 4: Direcciones sin número (Person.Address)")
    # FIX: la condición correcta es IS NOT NULL para obtener registros a evaluar,
    # pero el filtro de "sin número" se hace en pandas de forma vectorizada.
    df = pd.read_sql_query(
        "SELECT AddressLine1 FROM Person.Address WHERE AddressLine1 IS NOT NULL",
        sql_conn,
    )
    # Vectorizado: True cuando NO hay dígito en la cadena
    sin_numero = (~df["AddressLine1"].str.contains(r"\d", na=False)).sum()
    if sin_numero:
        msg = f"  {sin_numero:,} direcciones sin número"
        issues.append(msg)
        log.warning(msg)
    else:
        log.info("  Todas las direcciones tienen número.")

    log.info("CHECK 5: Top 5 ciudades en Person.Address")
    df_city = pd.read_sql_query(
        "SELECT City, COUNT(*) AS cnt FROM Person.Address GROUP BY City ORDER BY cnt DESC",
        sql_conn,
    )
    log.info("\n%s", df_city.head(5).to_string(index=False))

    return issues


# ──────────────────────────────────────────────
# 5. MIGRACIÓN REAL  (chunk → transform → insert)
# ──────────────────────────────────────────────

def get_column_names(sql_conn: pyodbc.Connection, table: str) -> list[str]:
    """Devuelve los nombres de columna de la tabla origen."""
    schema, tbl = table.split(".")
    cursor = sql_conn.cursor()
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        schema,
        tbl,
    )
    return [row[0] for row in cursor.fetchall()]


def ensure_pg_table(pg_conn, pg_table: str, columns: list[str]) -> None:
    """
    Crea la tabla en PostgreSQL si no existe.
    Usa TEXT para todas las columnas como tipo genérico seguro.
    Para producción, sustituye por un mapeo de tipos real.
    """
    cols_def = ", ".join(f'"{c}" TEXT' for c in columns)
    ddl = f'CREATE TABLE IF NOT EXISTS "{pg_table}" ({cols_def});'
    with pg_conn.cursor() as cur:
        cur.execute(ddl)
    pg_conn.commit()
    log.info("  Tabla '%s' lista en PostgreSQL.", pg_table)


def migrate_table(
    sql_conn: pyodbc.Connection,
    pg_conn: psycopg2.extensions.connection,
    src_table: str,
) -> int:
    """
    Migra src_table de SQL Server a PostgreSQL en chunks.
    Devuelve el número total de filas insertadas.
    """
    pg_table = PG_TABLE_MAP[src_table]
    columns = get_column_names(sql_conn, src_table)
    ensure_pg_table(pg_conn, pg_table, columns)

    col_list = ", ".join(f'"{c}"' for c in columns)
    insert_sql = f'INSERT INTO "{pg_table}" ({col_list}) VALUES %s'

    query = f"SELECT * FROM {src_table}"
    total_inserted = 0
    chunk_num = 0
    consecutive_errors = 0

    log.info("  Leyendo '%s' en chunks de %d filas...", src_table, CHUNK_SIZE)

    for chunk in pd.read_sql_query(query, sql_conn, chunksize=CHUNK_SIZE):
        chunk_num += 1
        rows = [tuple(row) for row in chunk.itertuples(index=False, name=None)]

        try:
            with pg_conn.cursor() as cur:
                execute_values(cur, insert_sql, rows, page_size=CHUNK_SIZE)
            pg_conn.commit()
            total_inserted += len(rows)
            consecutive_errors = 0
            log.info(
                "    Chunk %d: %d filas insertadas (acumulado: %d)",
                chunk_num, len(rows), total_inserted,
            )
        except Exception as e:
            pg_conn.rollback()
            consecutive_errors += 1
            log.error(
                "    Error en chunk %d de '%s': %s", chunk_num, src_table, e
            )
            if consecutive_errors >= MAX_ERRORS:
                raise RuntimeError(
                    f"Abortando '{src_table}': {MAX_ERRORS} errores consecutivos."
                ) from e

    return total_inserted


# ──────────────────────────────────────────────
# 6. POST-MIGRATION CHECKS
# ──────────────────────────────────────────────

def verify_row_counts(
    sql_conn: pyodbc.Connection,
    pg_conn: psycopg2.extensions.connection,
    baseline: dict,
) -> bool:
    """Compara filas origen vs destino. Devuelve True si todo cuadra."""
    log.info("=" * 55)
    log.info("VERIFICACIÓN FINAL: Conteo origen vs destino")
    log.info("=" * 55)
    all_ok = True
    for src_table in TABLES_ORDERED:
        pg_table = PG_TABLE_MAP[src_table]
        with pg_conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{pg_table}"')
            pg_count = cur.fetchone()[0]
        src_count = baseline[src_table]
        status = "✓" if pg_count == src_count else "✗ DIFERENCIA"
        log.info(
            "  %-35s  src=%d  pg=%d  %s",
            src_table, src_count, pg_count, status,
        )
        if pg_count != src_count:
            all_ok = False
    return all_ok


# ──────────────────────────────────────────────
# 7. PUNTO DE ENTRADA
# ──────────────────────────────────────────────

def main() -> None:
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║   Inicio de migración SQL Server → PostgreSQL    ║")
    log.info("╚══════════════════════════════════════════════════╝")

    # — Conexiones (una sola vez cada una) —
    sql_conn = connect_sql_server()
    pg_conn  = connect_postgres()

    try:
        # — Pre-migration —
        baseline = check_row_counts(sql_conn)
        issues   = check_data_quality(sql_conn)

        if issues:
            log.warning("Se encontraron %d issues de calidad (ver arriba).", len(issues))
            log.warning("La migración continuará pero revisa los datos después.")

        # — Migración tabla por tabla en orden correcto —
        log.info("=" * 55)
        log.info("MIGRACIÓN DE DATOS")
        log.info("=" * 55)
        total_global = 0
        for table in TABLES_ORDERED:
            log.info("→ Migrando: %s", table)
            n = migrate_table(sql_conn, pg_conn, table)
            log.info("  Completado: %d filas migradas.\n", n)
            total_global += n

        log.info("Total de filas migradas: %d", total_global)

        # — Post-migration verification —
        ok = verify_row_counts(sql_conn, pg_conn, baseline)
        if ok:
            log.info("✓ Verificación exitosa. Migración completada.")
        else:
            log.error("✗ Diferencias encontradas. Revisa los logs.")

    except Exception as e:
        log.exception("Error fatal durante la migración: %s", e)
        raise

    finally:
        # — Cierre limpio de conexiones —
        sql_conn.close()
        pg_conn.close()
        log.info("Conexiones cerradas.")


if __name__ == "__main__":
    main()