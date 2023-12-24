#--------------------------------------------------------------------------------------------------------------------------
# This code will set up a basic Flask project structure and then interact with a PostegreSQL database through psycopg2 API
#--------------------------------------------------------------------------------------------------------------------------
#--------------------------------------------------------------------------------------------------------------------------
# Author: Igor Cleto S. De Araujo
# Version: 0.0.1
#--------------------------------------------------------------------------------------------------------------------------

# Importing necessary libraries
from flask import Flask, request, jsonify
import csv
import os
import signal
import threading
import time
import psycopg2
from psycopg2 import sql
import json

# Database connection parameters
DB_HOST = "localhost"
DB_NAME = "raw"
DB_USER = "postgres"
DB_PASS = "admin"
DB_PORT = "5433"

#--------------------------------------------------------------------------------------------------------------------------
# Methods
#--------------------------------------------------------------------------------------------------------------------------

def create_database_if_not_exists(db_name, db_user, db_pass, db_host, db_port):
    """
    Connects to the PostgreSQL server and creates the target database if it does not exist.

    :param db_user: Database username
    :param db_pass: Database password
    :param db_host: Database host address
    :param db_port: Database port
    :param target_db: Name of the database to check and create if it doesn't exist
    """

    conn = psycopg2.connect(user=db_user, password=db_pass, host=db_host, port=db_port, dbname='postgres')
    conn.autocommit = True

    with conn.cursor() as cursor:
        # Check if the target database exists
        cursor.execute(sql.SQL("SELECT 1 FROM pg_database WHERE datname = %s"), (db_name,))
        exists = cursor.fetchone()

        # If the database does not exist, create it
        if not exists:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))

    conn.close()

def create_table_and_insert_data(db_name, db_user, db_pass, db_host, db_port, file_path, file_name, overwrite=True):
    """
    Creates a table in the PostgreSQL database based on the CSV file's headers and inserts data.
    Can optionally overwrite the existing table or append to it.

    :param db_name: Database name
    :param db_user: Database username
    :param db_pass: Database password
    :param db_host: Database host address
    :param db_port: Database port
    :param file_path: Path to the CSV file
    :param file_name: Name of the file (used to name the table)
    :param overwrite: If True, overwrite existing table; if False, append to it
    """

    # Connect to the database
    conn = psycopg2.connect(dbname=db_name, user=db_user, password=db_pass, host=db_host, port=db_port)
    conn.autocommit = True

    with conn.cursor() as cursor, open(file_path, mode='r') as csv_file:
        reader = csv.reader(csv_file)
        headers = next(reader)  # Assuming the first row is the header
        table_name = os.path.splitext(file_name)[0]  # Table name is the filename without extension

        if overwrite:
            # Drop the table if it exists and then create it
            cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table_name)))
            cursor.execute(sql.SQL("CREATE TABLE {} ({})").format(
                sql.Identifier(table_name),
                sql.SQL(', ').join([sql.Identifier(header) + sql.SQL(' VARCHAR') for header in headers])
            ))

        # Insert data
        for row in reader:
            placeholders = sql.SQL(', ').join(sql.Placeholder() * len(row))
            insert_query = sql.SQL("INSERT INTO {} VALUES ({})").format(sql.Identifier(table_name), placeholders)
            cursor.execute(insert_query, row)

    conn.close()

def transform_data_to_bronze(raw_db_name, raw_table_name, db_user, db_pass, db_host, db_port, bronze_db_name='bronze'):
    """
    Transforms data from the 'raw' database table and loads it into the 'bronze' database.

    :param raw_db_name: Name of the 'raw' database
    :param bronze_db_name: Name of the 'bronze' database
    :param db_user: Database username
    :param db_pass: Database password
    :param db_host: Database host address
    :param db_port: Database port
    :param raw_table_name: Name of the table in the 'raw' database to transform
    """

    # Connect to the raw database
    conn_raw = psycopg2.connect(dbname=raw_db_name, user=db_user, password=db_pass, host=db_host, port=db_port)
    conn_raw.autocommit = True

    # Connect to the bronze database
    conn_bronze = psycopg2.connect(dbname=bronze_db_name, user=db_user, password=db_pass, host=db_host, port=db_port)
    conn_bronze.autocommit = True

    with conn_raw.cursor() as cursor_raw, conn_bronze.cursor() as cursor_bronze:
        # Execute CTE on raw database
        cte_query = sql.SQL("""
            WITH cte AS (
                SELECT
                    CAST(SPLIT_PART(datetime, ' ', 1) AS DATE) AS date,
                    CAST(SPLIT_PART(datetime, ' ', 2) AS TIME) AS time,
                    CAST(TRIM(SPLIT_PART(SPLIT_PART(origin_coord, '(', 2), ' ', 1)) AS FLOAT) AS origin_latitude,
                    CAST(TRIM(SPLIT_PART(SPLIT_PART(origin_coord, '(', 2), ' ', 2), ')') AS FLOAT) AS origin_longitude,
                    CAST(TRIM(SPLIT_PART(SPLIT_PART(destination_coord, '(', 2), ' ', 1)) AS FLOAT) AS destination_latitude,
                    CAST(TRIM(SPLIT_PART(SPLIT_PART(destination_coord, '(', 2), ' ', 2), ')') AS FLOAT) AS destination_longitude,
                    UPPER(region) AS region,
                    UPPER(datasource) AS datasource
                FROM {}
            )
            SELECT * FROM cte;
        """).format(sql.Identifier(raw_table_name))

        cursor_raw.execute(cte_query)

        # Fetch the transformed data
        transformed_data = cursor_raw.fetchall()

        # Define the structure of the bronze table
        cursor_bronze.execute("""
            CREATE TABLE IF NOT EXISTS bronze_trips (
                date DATE,
                time TIME,
                origin_latitude FLOAT,
                origin_longitude FLOAT,
                destination_latitude FLOAT,
                destination_longitude FLOAT,
                region VARCHAR,
                datasource VARCHAR
            );
        """)

        # Insert the transformed data into the bronze table
        insert_query = """
            INSERT INTO bronze_trips (
                date, time, origin_latitude, origin_longitude, 
                destination_latitude, destination_longitude, region, datasource
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        cursor_bronze.executemany(insert_query, transformed_data)

    # Close the connections
    conn_raw.close()
    conn_bronze.close()


    conn_silver.close()

def create_or_fetch_weekly_average_trips(db_user, db_pass, db_host, db_port, bronze_table_name, bronze_db_name='bronze',create_new=False):
    """
    Either creates a new table in the 'silver' database for the weekly average number of trips or fetches the data.

    :param db_user: Database username
    :param db_pass: Database password
    :param db_host: Database host address
    :param db_port: Database port
    :param bronze_db_name: Name of the bronze database
    :param bronze_table_name: Name of the table in the bronze database
    :param create_new: Flag to indicate whether to create a new table (True) or fetch data (False)
    :return: JSON payload of the data if create_new is False
    """

    # Connect to the bronze database
    conn = psycopg2.connect(dbname=bronze_db_name, user=db_user, password=db_pass, host=db_host, port=db_port)
    
    if create_new:
        # Logic to create a new table
        pass  # Replace with actual logic to create a new table
    else:
        # Fetch data logic
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    DATE_TRUNC('week', date) AS week,
                    COUNT(*) / COUNT(DISTINCT DATE_TRUNC('week', date)) AS weekly_avg_trips
                FROM {}
                GROUP BY week;
            """.format(bronze_table_name))  # Assuming 'date' column exists in the table

            result = cursor.fetchall()
            # Convert the result to JSON
            result_json = json.dumps([{"week": row[0].strftime("%Y-%m-%d"), "weekly_avg_trips": row[1]} for row in result])

        conn.close()
        return result_json

# Example usage:
# data_json = create_or_fetch_weekly_average_trips(DB_USER, DB_PASS, DB_HOST, DB_PORT, 'bronze', 'bronze_table_name', create_new=False)

def restart_server():
    time.sleep(1)  # Short delay to ensure the response is sent
    os.kill(os.getpid(), signal.SIGINT)

#--------------------------------------------------------------------------------------------------------------------------
# Flask Data Ingestion API
#--------------------------------------------------------------------------------------------------------------------------

# Initialize the Flask application
app = Flask(__name__)

#--------------------------------------------------------------------------------------------------------------------------
# Create the API Endpoints
@app.route('/upload-csv', methods=['POST'])
def upload_csv():

    # Check if a file is part of the request
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']

    # If the user does not select a file, the browser submits an empty file without a filename.
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    if file and file.filename.endswith('.csv'):
        # Parse the CSV file
        try:
            # Temporary save the file
            file_path = os.path.join('temp', file.filename)
            file.save(file_path)
            table_name = os.path.splitext(file.filename)[0]  # Table name is the filename without extension

            # Create the postegresql RAW Database
            create_database_if_not_exists(db_user=DB_USER, db_pass=DB_PASS, db_host=DB_HOST, db_port=DB_PORT,db_name=DB_NAME)

            # Insert data into the RAW Database
            create_table_and_insert_data(db_user=DB_USER, db_pass=DB_PASS, db_host=DB_HOST, db_port=DB_PORT, db_name=DB_NAME, file_path=file_path, file_name=table_name, overwrite=True)

            # Transform raw data into bronze data
            create_database_if_not_exists(db_user=DB_USER, db_pass=DB_PASS, db_host=DB_HOST, db_port=DB_PORT,db_name='bronze')
            transform_data_to_bronze(raw_db_name=DB_NAME,raw_table_name=table_name,bronze_db_name='bronze',db_user=DB_USER,db_pass=DB_PASS,db_host=DB_HOST,db_port=DB_PORT)

            bronze_table_name = f"bronze_{table_name}"
            create_silver_database_for_regions(db_user=DB_USER,db_pass=DB_PASS,db_host=DB_HOST,db_port=DB_PORT,bronze_db_name=bronze_table_name)

            # Cleanup
            os.remove(file_path)
            return jsonify({'message': 'File uploaded and saved to database successfully'}), 200

        except Exception as e:
            return jsonify({'error': str(e)}), 500

    else:
        return jsonify({'error': 'Invalid file format'}), 400

#--------------------------------------------------------------------------------------------------------------------------

@app.route('/weekly-average-trips', methods=['GET'])
def weekly_average_trips():
    
    create_db = request.args.get('create_db', 'true').lower() == 'true'
    file_name = request.args.get('file_name')

    if not file_name:
        return jsonify({'error': 'Missing file name'}), 400

    bronze_table_name = f"bronze_{file_name}"

    try:
        if create_db:
            # Create database,table and insert data
            create_database_if_not_exists(db_user=DB_USER, db_pass=DB_PASS, db_host=DB_HOST, db_port=DB_PORT,db_name='silver')

            create_or_fetch_weekly_average_trips(DB_USER, DB_PASS, DB_HOST, DB_PORT, 'bronze', bronze_table_name, create_new=True)

            message = 'Silver table created from bronze table successfully'

        else:
            # Fetch data as JSON
            data_json = create_or_fetch_weekly_average_trips(DB_USER, DB_PASS, DB_HOST, DB_PORT, 'bronze', bronze_table_name, create_new=False)
            return jsonify({'data': data_json})

        return jsonify({'message': message}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Define your other functions here...
# - create_silver_table_from_bronze
# - create_or_fetch_weekly_average_trips
# - etc.

if __name__ == '__main__':
    app.run(debug=True)


#--------------------------------------------------------------------------------------------------------------------------

@app.route('/restart-server', methods=['POST'])
def trigger_restart():
    # Start a separate thread to restart the server
    threading.Thread(target=restart_server).start()
    return jsonify({'message': 'Server restarting...'}), 200

#--------------------------------------------------------------------------------------------------------------------------
# Run the Flask app
if __name__ == '__main__':
    app.run(port=8000,debug=True)