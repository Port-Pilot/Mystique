import psycopg2, config
conn = psycopg2.connect(dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASSWORD, host=config.DB_HOST, port=config.DB_PORT)
cur = conn.cursor()
cur.execute('SELECT generated_patch FROM backport_benchmark_results WHERE id=2')
row = cur.fetchone()
print(row[0][:500] if row and row[0] else None)
