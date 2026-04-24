#Prefece

Este proyecto es para migrar datos de SQLServer a PostgreSQL
se usara

- SQL
- Phyton
- Jupiter Notebook

# Pseudocode

## High-level

1. Audit data in SQL Server (before migration)
2. Extract data from SQLServer Microsoft
3. Transform the data
4. Load the data in PostgreSQL
5. Validate the data (after migration)
6. Generate Validation report

## Low-Level
- Create a .env file
- Load env variables
- Connect to SQL Server (pyodbc)
- Connect to Postgres (pyscopg2)
- Audit the data
- For each table:
    - Get row count
    - Extract all rows 
    - Transform the column name to lowercase
    - Convert the data types
- Create tables in Postgres
- Load tables into Postgres
- Run Post data migration checks
- Prepare validation Report
